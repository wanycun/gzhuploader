#!/usr/bin/env python3
# GzhUploader 状态页 + 操作台服务（unix socket, fnOS 统一网关）
import base64
import json
import os
import re
import shutil
import sys
import urllib.request
import urllib.error
import urllib.parse
import hmac
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import UnixStreamServer

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.dirname(HERE)
WWW = os.path.join(TARGET, 'www')
APP_VERSION = '3.11.2'
SOCK = os.path.join(TARGET, 'app.sock')
PREFIX = '/app/gzhuploader'
API = 'https://api.weixin.qq.com'


def data_dir():
    return os.path.realpath(os.path.join(TARGET, 'data'))


def var_dir():
    return os.environ.get('TRIM_PKGVAR') or os.path.join(TARGET, 'var')


def load_config():
    return json.load(open(os.path.join(data_dir(), 'config.json')))


def egress_ip():
    try:
        req = urllib.request.Request('https://myip.ipip.net',
                                     headers={'User-Agent': 'curl/8'})
        txt = urllib.request.urlopen(req, timeout=8).read().decode('utf-8', 'ignore')
        m = re.search(r'(\d{1,3}(?:\.\d{1,3}){3})', txt)
        return m.group(1) if m else None
    except Exception:
        return None


def _wx_call_once(path, token=None, payload=None, timeout=20):
    url = API + path
    if token:
        url += ('&' if '?' in url else '?') + 'access_token=' + token
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=body,
                                 headers={'Content-Type': 'application/json'})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def wx_call(path, token, payload=None, timeout=20):
    r = _wx_call_once(path, token, payload, timeout)
    if isinstance(r, dict) and r.get('errcode') in (40001, 40014, 42001):
        fresh = get_token(force=True)
        if fresh and fresh != token:
            r = _wx_call_once(path, fresh, payload, timeout)
    return r


def _cached_token(path):
    try:
        d = json.load(open(path))
        tok = d.get('access_token')
        if tok and float(d.get('expires_at') or 0) > time.time() + 120:
            return tok
    except Exception:
        pass
    return None


def get_token(cfg=None, force=False):
    if not force:
        tok = _cached_token(os.path.join(var_dir(), 'token.json'))
        if tok:
            return tok
        tok = _cached_token(os.path.join(data_dir(), 'token.json'))
        if tok:
            return tok
    st = load_settings()
    url = (API + '/cgi-bin/token?grant_type=client_credential&appid=%s&secret=%s'
           % (st.get('wx_appid', ''), st.get('wx_secret', '')))
    r = json.loads(urllib.request.urlopen(url, timeout=15).read())
    tok = r.get('access_token')
    if tok:
        try:
            os.makedirs(var_dir(), exist_ok=True)
            json.dump({'access_token': tok,
                       'expires_at': time.time() + int(r.get('expires_in') or 7200)},
                      open(os.path.join(var_dir(), 'token.json'), 'w'))
        except Exception:
            pass
    return tok


def log_run(line):
    stamp = __import__('time').strftime('%Y-%m-%d %H:%M:%S')
    entry = '%s %s' % (stamp, line)
    for p in (os.path.join(data_dir(), 'runs.log'),
              os.path.join(var_dir(), 'runs.log')):
        try:
            with open(p, 'a') as f:
                f.write(entry + '\n')
            break
        except Exception:
            continue


def parse_front_matter(text):
    meta, body = {}, text
    if text.startswith('---'):
        end = text.find('\n---', 3)
        if end > 0:
            for line in text[3:end].strip().splitlines():
                if ':' in line:
                    k, v = line.split(':', 1)
                    meta[k.strip()] = v.strip().strip('"').strip("'")
            body = text[end + 4:].lstrip('\n')
    return meta, body


def md_to_html(md):
    out, in_list = [], False
    for raw in md.splitlines():
        line = raw.strip()
        if line.startswith('![') and '](' in line and line.endswith(')'):
            src = line[line.index('](') + 2:-1]
            out.append('<p style="text-align:center"><img src="%s" style="max-width:100%%"></p>' % src)
            continue
        if line.startswith('- ') or line.startswith('* '):
            if not in_list:
                out.append('<ul>')
                in_list = True
            out.append('<li>%s</li>' % inline(line[2:]))
            continue
        if in_list:
            out.append('</ul>')
            in_list = False
        if line.startswith('### '):
            out.append('<h3>%s</h3>' % inline(line[4:]))
        elif line.startswith('## '):
            out.append('<h2>%s</h2>' % inline(line[3:]))
        elif line.startswith('# '):
            out.append('<h1>%s</h1>' % inline(line[2:]))
        elif line.startswith('> '):
            out.append('<blockquote><p>%s</p></blockquote>' % inline(line[2:]))
        elif line:
            out.append('<p>%s</p>' % inline(line))
    if in_list:
        out.append('</ul>')
    return '\n'.join(out)


def inline(s):
    s = s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    s = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', s)
    s = re.sub(r'!\[(.*?)\]\((.*?)\)', r'<img src="\2" alt="\1" style="max-width:100%">', s)
    s = re.sub(r'\[(.+?)\]\((https?://.*?)\)', r'<a href="\2">\1</a>', s)
    return s


def upload_media_img(token, path):
    boundary = '----gzhuploaderboundary'
    data = open(path, 'rb').read()
    name = os.path.basename(path)
    body = (('--%s\r\nContent-Disposition: form-data; name="media"; filename="%s"\r\n'
             'Content-Type: application/octet-stream\r\n\r\n' % (boundary, name)).encode()
            + data + ('\r\n--%s--\r\n' % boundary).encode())
    req = urllib.request.Request(
        API + '/cgi-bin/media/uploadimg?access_token=' + token, data=body,
        headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def upload_thumb(token, path):
    boundary = '----gzhuploaderboundary'
    data = open(path, 'rb').read()
    name = os.path.basename(path)
    body = (('--%s\r\nContent-Disposition: form-data; name="media"; filename="%s"\r\n'
             'Content-Type: application/octet-stream\r\n\r\n' % (boundary, name)).encode()
            + data + ('\r\n--%s--\r\n' % boundary).encode())
    req = urllib.request.Request(
        API + '/cgi-bin/material/add_material?type=thumb&access_token=' + token,
        data=body,
        headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def do_upload(pkg):
    ip = egress_ip()
    cfg = load_settings()
    if not cfg.get('wx_secret'):
        return {'ok': False, 'stage': 'config', 'error': 'AppSecret 未配置', 'egress_ip': ip}
    pdir = os.path.join(data_dir(), 'jobs', pkg)
    if not os.path.isdir(pdir):
        return {'ok': False, 'stage': 'package', 'error': '文章包不存在: %s' % pkg, 'egress_ip': ip}
    token = get_token()
    if not token:
        return {'ok': False, 'stage': 'token', 'error': '取 access_token 失败（可能 IP 白名单，当前出口 %s）' % ip, 'egress_ip': ip}
    md_path = os.path.join(pdir, 'article.md')
    meta, body = {}, ''
    if os.path.exists(md_path):
        meta, body = parse_front_matter(open(md_path, encoding='utf-8').read())
    html_path = os.path.join(pdir, 'content.html')
    if os.path.exists(html_path):
        content = open(html_path, encoding='utf-8').read()
    else:
        content = md_to_html(body)
    # 正文图片：本地 src 逐张换微信 URL
    def repl(m):
        src = m.group(1)
        if src.startswith('http://') or src.startswith('https://'):
            return m.group(0)
        fpath = os.path.join(pdir, src)
        if not os.path.exists(fpath):
            return m.group(0)
        r = upload_media_img(token, fpath)
        if r.get('url'):
            return 'src="%s"' % r['url']
        raise RuntimeError('正文图上传失败 %s: %s' % (src, r))
    content = re.sub(r'src="([^"]+)"', repl, content)
    cover_name = meta.get('cover') or 'cover.png'
    cover_path = os.path.join(pdir, cover_name)
    if not os.path.exists(cover_path):
        return {'ok': False, 'stage': 'cover', 'error': '封面不存在: %s' % cover_name, 'egress_ip': ip}
    r = upload_thumb(token, cover_path)
    thumb = r.get('media_id')
    if not thumb:
        return {'ok': False, 'stage': 'thumb', 'error': '封面上传失败: %s' % r, 'egress_ip': ip}
    article = {
        'title': meta.get('title', pkg),
        'author': meta.get('author', 'CHUN'),
        'digest': meta.get('digest', ''),
        'content': content,
        'thumb_media_id': thumb,
        'need_open_comment': 1,
        'only_fans_can_comment': 0,
    }
    r = wx_call('/cgi-bin/draft/add', token=token, payload={'articles': [article]})
    if r.get('media_id'):
        log_run('OK pkg=jobs/%s draft_media_id=%s ip=%s (via app)' % (pkg, r['media_id'], ip))
        return {'ok': True, 'draft_media_id': r['media_id'], 'egress_ip': ip}
    log_run('FAIL pkg=jobs/%s err=%s (via app)' % (pkg, r))
    return {'ok': False, 'stage': 'draft_add', 'error': str(r), 'egress_ip': ip}




# ---------- 2.1: AI 文案生成 / 编辑 / 默认封面 ----------

GEN_SYSTEM = """你是公众号「CHUN折腾」的写作助手。作者 CHUN 是项目驱动的折腾者，原则是「我不评测，我只把东西做出来」。按以下口径写一篇可直接改的初稿：
- 第一人称：「我」是作者；动手的 AI 助手称 Muse、指代用「它」。
- 标题公式：[我的处境或数字] + [我做了什么] + [结果或反转]。
- 结构：处境开头 → 过程（关键步骤）→ 翻车与未解决（如有就照实写，没有就跳过）→ 结尾可以反高潮，不强行全胜。
- 去技术化，技术细节说人话。
- 绝不编造具体数字、时长、价格；拿不准的地方写「待实测」或略过。
- 文末留一句钩子：回复关键词领取清单，关键词用主题的短名。
只输出一个 JSON 对象：{"title": "标题", "digest": "一句话摘要", "md": "正文 Markdown，从正文开始，不要再写标题行"}。不要输出 JSON 以外的任何内容。"""


def ai_path():
    return os.path.join(var_dir(), 'ai.json')


def load_ai():
    try:
        return json.load(open(ai_path()))
    except Exception:
        return {}


def do_generate(topic, notes):
    st = load_settings()
    key = (st.get('ds_key') or '').strip()
    if not key:
        return {'ok': False, 'stage': 'ai_config', 'error': '先在设置页填 DeepSeek API key'}
    model = st.get('ds_model') or 'deepseek-chat'
    user_msg = '主题：%s' % topic
    if notes:
        user_msg += '\n备注要点：%s' % notes
    sys_prompt = GEN_SYSTEM.replace('CHUN折腾', st.get('label') or '公众号')
    sys_prompt = sys_prompt.replace('作者 CHUN', '作者' + (st.get('author') or ''))
    payload = {'model': model,
               'messages': [{'role': 'system', 'content': sys_prompt},
                            {'role': 'user', 'content': user_msg}],
               'temperature': 0.7,
               'response_format': {'type': 'json_object'}}
    req = urllib.request.Request(
        'https://api.deepseek.com/chat/completions',
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json',
                 'Authorization': 'Bearer ' + key})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=150).read())
    except urllib.error.HTTPError as e:
        return {'ok': False, 'stage': 'deepseek',
                'error': 'DeepSeek 返回 %s: %s' % (e.code, e.read()[:300])}
    except Exception as e:
        return {'ok': False, 'stage': 'deepseek', 'error': str(e)}
    content = r['choices'][0]['message']['content']
    try:
        art = json.loads(content)
        title = art.get('title') or topic
        digest = art.get('digest') or ''
        md = art.get('md') or ''
    except Exception:
        title, digest, md = topic, '', content
    import time as _t
    name = 'gen-' + _t.strftime('%Y%m%d-%H%M%S')
    pdir = os.path.join(data_dir(), 'jobs', name)
    os.makedirs(pdir)
    fm = ('---\ntitle: %s\nauthor: %s\ndigest: %s\ncover: cover.png\n---\n\n%s\n'
          % (title, st.get('author', ''), digest, md))
    with open(os.path.join(pdir, 'article.md'), 'w', encoding='utf-8') as f:
        f.write(fm)
    log_run('GEN pkg=jobs/%s topic=%s (via app)' % (name, topic))
    return {'ok': True, 'name': name, 'title': title}


def write_png(path, w, h, pixel):
    import zlib as _z
    import struct as _st
    rows = []
    for y in range(h):
        row = bytearray(bytes([0]))
        for x in range(w):
            row += bytes(pixel(x, y))
        rows.append(bytes(row))
    raw = b''.join(rows)

    def chunk(tag, data):
        c = _st.pack('>I', len(data)) + tag + data
        c += _st.pack('>I', _z.crc32(tag + data) & 0xffffffff)
        return c
    png = (bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
           + chunk(b'IHDR', _st.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
           + chunk(b'IDAT', _z.compress(raw, 6))
           + chunk(b'IEND', b''))
    with open(path, 'wb') as f:
        f.write(png)


def default_cover(pdir):
    W, H = 900, 383
    bg = (10, 42, 94)
    grid = (23, 66, 128)
    white = (234, 244, 255)
    cyan = (64, 196, 240)
    cx, cy, r = W // 2, H // 2, 92

    def pixel(x, y):
        if x % 60 == 0 or y % 60 == 0:
            c = grid
        else:
            c = bg
        d2 = (x - cx) * (x - cx) + (y - cy) * (y - cy)
        if abs(d2 - r * r) < 700:
            c = white
        if (y > cy - 58 and y < cy + 58 and x > cx - 16 and x < cx + 16
                and y < cy + (x - cx) and y > cy - (x - cx)):
            c = cyan
        if cy + 118 < y < cy + 128 and cx - 90 < x < cx + 90:
            c = white
        return c
    write_png(os.path.join(pdir, 'cover.png'), W, H, pixel)




# ---------- 3.0: unified settings / spec / typeset ----------

def settings_path():
    return os.path.join(var_dir(), 'settings.json')


def load_settings():
    st = {}
    try:
        st = json.load(open(settings_path()))
    except Exception:
        st = {}
    if not st.get('wx_secret'):
        try:
            legacy = load_config()
            if legacy.get('appid'):
                st.setdefault('wx_appid', legacy.get('appid'))
            if legacy.get('secret'):
                st.setdefault('wx_secret', legacy.get('secret'))
        except Exception:
            pass
    try:
        ai = load_ai()
        if ai.get('key'):
            st.setdefault('ds_key', ai.get('key'))
        if ai.get('model'):
            st.setdefault('ds_model', ai.get('model'))
    except Exception:
        pass
    st.setdefault('author', 'CHUN')
    st.setdefault('label', 'CHUN折腾')
    st.setdefault('intro', '我不评测，我只把东西做出来')
    st.setdefault('ds_model', 'deepseek-chat')
    return st


def save_settings(st):
    os.makedirs(var_dir(), exist_ok=True)
    json.dump(st, open(settings_path(), 'w'), ensure_ascii=False)


def wx_config():
    st = load_settings()
    if st.get('wx_secret'):
        return {'appid': st.get('wx_appid', ''), 'secret': st.get('wx_secret', '')}
    try:
        return json.load(open(os.path.join(data_dir(), 'config.json')))
    except Exception:
        return {'appid': st.get('wx_appid', ''), 'secret': ''}


def md_to_spec(meta, body, st):
    sections = []
    cur = {'heading': '', 'blocks': []}
    sections.append(cur)
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line:
            continue
        if line.startswith('## '):
            cur = {'heading': line[3:].strip(), 'blocks': []}
            sections.append(cur)
            continue
        if line.startswith('# '):
            cur = {'heading': line[2:].strip(), 'blocks': []}
            sections.append(cur)
            continue
        if line.startswith('![') and '](' in line and line.endswith(')'):
            src = line[line.index('](') + 2:-1]
            cur['blocks'].append({'t': 'img', 'src': src, 'cap': ''})
            continue
        if line.startswith('> '):
            cur['blocks'].append({'t': 'quote', 'x': line[2:], 'from': ''})
            continue
        if line.startswith('- ') or line.startswith('* '):
            items = [line[2:]]
            while i < len(lines) and (lines[i].strip().startswith('- ') or lines[i].strip().startswith('* ')):
                items.append(lines[i].strip()[2:])
                i += 1
            cur['blocks'].append({'t': 'list', 'items': items})
            continue
        cur['blocks'].append({'t': 'p', 'x': line})
    sections = [x for x in sections if x['blocks'] or x['heading']]
    import time as _t
    return {'theme': 'electric-blueprint',
            'label': st.get('label', ''),
            'date': _t.strftime('%Y.%m'),
            'title': meta.get('title', ''),
            'summary': meta.get('digest', ''),
            'author': st.get('author', ''),
            'intro': st.get('intro', ''),
            'sections': sections}


def package_spec(name, st):
    pdir = os.path.join(data_dir(), 'jobs', name)
    sp_path = os.path.join(pdir, 'spec.json')
    if os.path.exists(sp_path):
        try:
            return json.load(open(sp_path, encoding='utf-8'))
        except Exception:
            pass
    mp = os.path.join(pdir, 'article.md')
    meta, body = {}, ''
    if os.path.exists(mp):
        meta, body = parse_front_matter(open(mp, encoding='utf-8').read())
    return md_to_spec(meta, body, st)


IMG_EXTS = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
            '.gif': 'image/gif', '.webp': 'image/webp'}


def pkgimg_url(name, src):
    return '/app/gzhuploader/pkgimg?name=%s&f=%s' % (
        name, urllib.parse.quote(os.path.basename(src)))


def rewrite_spec_imgs(obj, name):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == 'src' and isinstance(v, str) and v and \
                    not v.startswith(('http://', 'https://', 'data:', '/')):
                obj[k] = pkgimg_url(name, v)
            else:
                rewrite_spec_imgs(v, name)
    elif isinstance(obj, list):
        for v in obj:
            rewrite_spec_imgs(v, name)


def rewrite_html_imgs(html, name):
    def rep(m):
        return m.group(1) + pkgimg_url(name, m.group(2)) + m.group(3)
    return re.sub(r'(<img[^>]+src=")(?!http://|https://|data:|/)([^"]+)(")',
                  rep, html)


def unrewrite_html_imgs(html):
    return re.sub(r'/app/gzhuploader/pkgimg\?name=[^"\s]*?(?:&amp;|&)f=([^"\s&]+)',
                  lambda m: urllib.parse.unquote(m.group(1)), html)


EDIT_JS = '<script>\n(function () {\n  var PKG = __PKG_JSON__;\n  var BASE = \'/app/gzhuploader\';\n  function findActions() {\n    var btns = document.querySelectorAll(\'button, a\');\n    for (var i = 0; i < btns.length; i++) {\n      if ((btns[i].textContent || \'\').indexOf(\'保存排版\') >= 0) return btns[i];\n    }\n    return null;\n  }\n  function mount() {\n    var saveBtn = findActions();\n    var host = saveBtn && saveBtn.parentElement;\n    if (!host || document.getElementById(\'gzh-edit-btn\')) return;\n    var btn = document.createElement(\'button\');\n    btn.id = \'gzh-edit-btn\';\n    btn.textContent = \'编辑文字 / 图片\';\n    btn.className = saveBtn.className || \'\';\n    btn.style.cssText = \'background:#EEF2FF;border:1px solid #3B5BFD;color:#2F4BE0;cursor:pointer;margin-left:8px\';\n    btn.addEventListener(\'click\', openEditor);\n    host.appendChild(btn);\n  }\n  function ta(val, rows) {\n    var t = document.createElement(\'textarea\');\n    t.value = val || \'\';\n    t.rows = rows || 3;\n    t.style.cssText = \'width:100%;box-sizing:border-box;border:1px solid #E3E6EA;border-radius:8px;padding:8px;font-size:13px;line-height:1.6;margin:4px 0 10px\';\n    return t;\n  }\n  function label(txt) {\n    var d = document.createElement(\'div\');\n    d.textContent = txt;\n    d.style.cssText = \'font-size:12px;color:#8A94A6;margin-top:8px\';\n    return d;\n  }\n  function openEditor() {\n    var old = document.getElementById(\'gzh-editor\');\n    if (old) { old.remove(); return; }\n    fetch(BASE + \'/spec-get?name=\' + encodeURIComponent(PKG)).then(function (r) { return r.json(); }).then(function (res) {\n      if (!res.ok) { alert(\'读取排版规格失败\'); return; }\n      var spec = res.spec, files = res.files || [];\n      var panel = document.createElement(\'div\');\n      panel.id = \'gzh-editor\';\n      panel.style.cssText = \'position:fixed;top:0;right:0;bottom:0;width:min(430px,94vw);background:#fff;z-index:10001;box-shadow:-8px 0 28px rgba(16,24,40,.18);padding:16px;overflow-y:auto;box-sizing:border-box;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif\';\n      var h = document.createElement(\'div\');\n      h.innerHTML = \'<b style="font-size:15px">编辑文字 / 图片</b><div style="font-size:12px;color:#8A94A6;margin-top:4px">改动存入排版规格;保存后页面刷新,记得再点一次「保存排版」让文章定稿。标题与封面在「编辑」页改。</div>\';\n      panel.appendChild(h);\n      var fields = [];\n      (spec.sections || []).forEach(function (sec, si) {\n        panel.appendChild(label(\'小节 \' + (si + 1) + \' 标题\'));\n        var fH = ta(sec.heading || \'\', 2); panel.appendChild(fH);\n        fields.push({ get: function () { sec.heading = fH.value; } });\n        (sec.blocks || []).forEach(function (blk, bi) {\n          var where = \'小节 \' + (si + 1) + \' · 第 \' + (bi + 1) + \' 段\';\n          if (blk.t === \'img\') {\n            panel.appendChild(label(where + \'(图片)\'));\n            var sel = document.createElement(\'select\');\n            sel.style.cssText = \'width:100%;box-sizing:border-box;border:1px solid #E3E6EA;border-radius:8px;padding:8px;font-size:13px;margin:4px 0 6px;background:#fff\';\n            files.filter(function (f) { return /\\.(png|jpe?g|gif|webp)$/i.test(f); }).forEach(function (f) {\n              var o = document.createElement(\'option\'); o.value = f; o.textContent = f; if (f === blk.src) o.selected = true; sel.appendChild(o);\n            });\n            panel.appendChild(sel);\n            var up = document.createElement(\'input\');\n            up.type = \'file\'; up.accept = \'image/*\';\n            up.style.cssText = \'font-size:12px;margin:2px 0 6px\';\n            panel.appendChild(up);\n            var note = document.createElement(\'div\');\n            note.style.cssText = \'font-size:12px;color:#8A94A6;margin-bottom:10px\';\n            note.textContent = \'选包里现有的图,或上传一张新图(自动存进包里)。\';\n            panel.appendChild(note);\n            fields.push({ get: function () { blk.src = sel.value; }, file: up, sel: sel, blk: blk });\n            if (blk.cap !== undefined) {\n              panel.appendChild(label(\'图片说明\'));\n              var fCap = ta(blk.cap || \'\', 2); panel.appendChild(fCap);\n              fields.push({ get: function () { blk.cap = fCap.value; } });\n            }\n          } else if (blk.t === \'list\') {\n            panel.appendChild(label(where + \'(列表,一行一条)\'));\n            var fL = ta((blk.items || []).join(\'\\n\'), 4); panel.appendChild(fL);\n            fields.push({ get: function () { blk.items = fL.value.split(\'\\n\'); } });\n          } else if (blk.t === \'table\') {\n            panel.appendChild(label(where + \'(表格,首行表头,逗号分隔)\'));\n            var rows = [blk.head || []].concat(blk.rows || []).map(function (r) { return r.join(\',\'); }).join(\'\\n\');\n            var fT = ta(rows, 4); panel.appendChild(fT);\n            fields.push({ get: function () { var ls = fT.value.split(\'\\n\').filter(function (x) { return x.length; }); blk.head = ls.length ? ls[0].split(\',\') : []; blk.rows = ls.slice(1).map(function (x) { return x.split(\',\'); }); } });\n          } else {\n            panel.appendChild(label(where + (blk.t === \'quote\' ? \'(引用)\' : \'(段落)\')));\n            var fX = ta(blk.x || \'\', 4); panel.appendChild(fX);\n            fields.push({ get: function () { blk.x = fX.value; } });\n          }\n        });\n      });\n      var bar = document.createElement(\'div\');\n      bar.style.cssText = \'display:flex;gap:8px;margin-top:14px\';\n      var save = document.createElement(\'button\');\n      save.textContent = \'保存修改并刷新\';\n      save.style.cssText = \'flex:1;padding:11px;border:none;border-radius:10px;background:#3B5BFD;color:#fff;font-size:14px;cursor:pointer\';\n      var close = document.createElement(\'button\');\n      close.textContent = \'关闭\';\n      close.style.cssText = \'padding:11px 16px;border:1px solid #E3E6EA;border-radius:10px;background:#fff;font-size:14px;cursor:pointer\';\n      close.addEventListener(\'click\', function () { panel.remove(); });\n      save.addEventListener(\'click\', function () {\n        save.disabled = true; save.textContent = \'保存中…\';\n        var chain = Promise.resolve();\n        fields.forEach(function (f) {\n          f.get();\n          if (f.file && f.file.files && f.file.files[0]) {\n            var file = f.file.files[0];\n            chain = chain.then(function () {\n              return new Promise(function (resolve) {\n                var rd = new FileReader();\n                rd.onload = function () {\n                  var b64 = String(rd.result).split(\',\')[1];\n                  fetch(BASE + \'/job-file\', { method: \'POST\', headers: { \'Content-Type\': \'application/json\' }, body: JSON.stringify({ name: PKG, filename: file.name, data_base64: b64 }) }).then(function () {\n                    f.blk.src = file.name;\n                    var o = document.createElement(\'option\'); o.value = file.name; o.textContent = file.name; o.selected = true; f.sel.appendChild(o);\n                    resolve();\n                  });\n                };\n                rd.readAsDataURL(file);\n              });\n            });\n          }\n        });\n        chain.then(function () {\n          return fetch(BASE + \'/save-spec\', { method: \'POST\', headers: { \'Content-Type\': \'application/json\' }, body: JSON.stringify({ name: PKG, spec: spec }) });\n        }).then(function () { location.reload(); }).catch(function () { save.disabled = false; save.textContent = \'保存失败,重试\'; });\n      });\n      bar.appendChild(save); bar.appendChild(close);\n      panel.appendChild(bar);\n      document.body.appendChild(panel);\n    });\n  }\n  var tries = 0;\n  var iv = setInterval(function () { mount(); if (document.getElementById(\'gzh-edit-btn\') || ++tries > 40) clearInterval(iv); }, 500);\n})();\n</script>\n'


def typeset_page(name):
    sys.path.insert(0, os.path.join(TARGET, 'typeset'))
    import render_engine as R
    st = load_settings()
    spec = package_spec(name, st)
    prepared = R.prepare_spec(spec)
    rewrite_spec_imgs(prepared, name)
    tpl = open(os.path.join(TARGET, 'typeset', 'interactive-template.html'),
               encoding='utf-8').read()
    title = (spec.get('title') or name).replace('**', '')
    page = tpl.replace('{{TITLE}}', title).replace(
        '{{SPEC_JSON}}',
        json.dumps(prepared, ensure_ascii=False).replace('</', '<\\/'))
    bridge = (
        '<style>body{margin-left:208px !important}'
        '.gzh-side{position:fixed;left:0;top:0;bottom:0;width:208px;background:#fff;'
        'border-right:1px solid #E3E6EA;padding:18px 12px;z-index:9998;'
        'font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}'
        '.gzh-side .lg{font-weight:700;font-size:15px;color:#111827;padding:2px 10px 16px}'
        '.gzh-side .lg b{color:#3B5BFD}'
        '.gzh-side a{display:block;color:#5B6470;text-decoration:none;font-size:14px;'
        'padding:8px 10px;border-radius:8px;margin-bottom:2px}'
        '.gzh-side a.on{background:#EEF2FF;color:#2F4BE0;font-weight:600}'
        '@media (max-width:760px){body{margin-left:0 !important;margin-top:52px}.gzh-side{top:0;bottom:auto;width:100%;height:52px;padding:0 10px;display:flex;align-items:center;gap:2px;border-right:none;border-bottom:1px solid #E3E6EA;overflow-x:auto}.gzh-side .lg{display:none}.gzh-side a{display:inline-block;margin:0;padding:6px 9px;white-space:nowrap}.app{flex-direction:column !important}.panel{width:100% !important;max-width:100% !important}#gzh-content{max-width:100% !important}#gzh-content>section{max-width:100% !important}.app{flex-direction:column !important;height:auto !important;max-height:none !important}.stage{flex:none !important;height:auto !important;max-height:none !important;overflow:visible !important;width:100% !important}.panel{flex:none !important;height:auto !important;max-height:none !important;overflow:visible !important;width:100% !important}}</style>'
        '<div class="gzh-side"><div class="lg">公众号助手</div>'
        '<a href="/app/gzhuploader/">仪表盘</a>'
        '<a href="/app/gzhuploader/packages">文章包</a>'
        '<a href="/app/gzhuploader/editor?name=' + name + '">编辑</a>'
        '<a class="on" href="javascript:void(0)">排版</a>'
        '<a href="/app/gzhuploader/generate">文案生成</a>'
        '<a href="/app/gzhuploader/drafts">草稿箱</a>'
        '<a href="/app/gzhuploader/logs">运行日志</a>'
        '<a href="/app/gzhuploader/about">关于</a>'
        '<a href="/app/gzhuploader/settings">设置</a></div>'
        '<script>function gzhBridgeSave(){'
        'var html = contentEl.innerHTML;'
        'fetch(\'/app/gzhuploader/save-typeset\', {method:\'POST\','
        'headers:{\'Content-Type\':\'application/json\'},'
        'body: JSON.stringify({name: \'' + name + '\', html: html})})'
        '.then(function(r){return r.json()}).then(function(r){'
        'alert(r.ok ? \'已保存排版，回列表点上传即用这版\' : \'保存失败\' + (r.error || \'\'));'
        '}).catch(function(e){ alert(\'保存失败\' + e); });}'
        'window.addEventListener(\'load\', function(){'
        'var b = document.getElementById(\'copyBtn\');'
        'if (b) { var nb = document.createElement(\'button\');'
        'nb.textContent = \'保存排版\'; nb.className = b.className;'
        'nb.style.cssText = \'background:#3B5BFD;border-color:#3B5BFD;color:#fff;margin-right:8px\';'
        'nb.onclick = gzhBridgeSave; b.parentNode.insertBefore(nb, b); }'
        '});</' + 'script>')
    inj = bridge + EDIT_JS.replace('__PKG_JSON__', json.dumps(name))
    if '</body>' in page:
        page = page.replace('</body>', inj + '</body>', 1)
    else:
        page += inj
    return page


API_PORT = 10039


def api_jobs():
    jd = os.path.join(data_dir(), 'jobs')
    jobs = []
    if os.path.isdir(jd):
        for name in sorted(os.listdir(jd)):
            pd = os.path.join(jd, name)
            if not os.path.isdir(pd):
                continue
            files = os.listdir(pd)
            jobs.append({'name': name, 'files': len(files),
                         'has_cover': 'cover.png' in files,
                         'has_content_html': 'content.html' in files})
    return jobs


def api_job_info(name):
    pdir = os.path.join(data_dir(), 'jobs', name)
    if not os.path.isdir(pdir):
        return None
    info = {'ok': True, 'name': name, 'files': sorted(os.listdir(pdir)),
            'has_content_html': os.path.exists(os.path.join(pdir, 'content.html')),
            'has_cover': os.path.exists(os.path.join(pdir, 'cover.png'))}
    mp = os.path.join(pdir, 'article.md')
    if os.path.exists(mp):
        raw = open(mp, encoding='utf-8').read()
        info['article_md'] = raw
        meta, body = parse_front_matter(raw)
        info['meta'] = meta
        info['body'] = body
    sp = os.path.join(pdir, 'spec.json')
    if os.path.exists(sp):
        try:
            info['spec'] = json.load(open(sp, encoding='utf-8'))
        except Exception:
            pass
    return info


def api_typeset(name, spec_in=None):
    pdir = os.path.join(data_dir(), 'jobs', name)
    if not os.path.isdir(pdir):
        return {'ok': False, 'error': '包不存在'}, 404
    if spec_in is not None:
        with open(os.path.join(pdir, 'spec.json'), 'w', encoding='utf-8') as f:
            json.dump(spec_in, f, ensure_ascii=False, indent=1)
    sys.path.insert(0, os.path.join(TARGET, 'typeset'))
    import render_engine as R
    spec = package_spec(name, load_settings())
    html = R.render_article(R.prepare_spec(spec))
    with open(os.path.join(pdir, 'content.html'), 'w', encoding='utf-8') as f:
        f.write(html)
    return {'ok': True, 'name': name, 'bytes': len(html)}, 200


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype='text/html; charset=utf-8', code=200):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        return self._send(json.dumps(obj, ensure_ascii=False).encode('utf-8'),
                          'application/json; charset=utf-8', code)

    def _read_json(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n <= 0:
            return {}
        return json.loads(self.rfile.read(n).decode('utf-8'))

    def _path(self):
        p = self.path.split('?')[0]
        if p.startswith(PREFIX):
            p = p[len(PREFIX):] or '/'
        return p

    def _query(self):
        q = {}
        if '?' in self.path:
            for kv in self.path.split('?', 1)[1].split('&'):
                if '=' in kv:
                    k, v = kv.split('=', 1)
                    from urllib.parse import unquote
                    q[k] = unquote(v)
        return q

    # ---- state ----
    def state(self):
        real = data_dir()
        st = {'ok': True, 'config_ok': False, 'egress_ip': None,
              'jobs': [], 'recent_runs': [], 'draft_count': None}
        try:
            cfg = wx_config()
            st['config_ok'] = bool(cfg.get('secret')) and cfg.get('secret') != 'SECRET_HERE'
            stx = load_settings()
            st['wx_set'] = bool(stx.get('wx_appid')) and bool(stx.get('wx_secret'))
            st['ds_set'] = bool(stx.get('ds_key'))
        except Exception:
            cfg = None
        jd = os.path.join(real, 'jobs')
        if os.path.isdir(jd):
            jobs = []
            for name in sorted(os.listdir(jd)):
                pd = os.path.join(jd, name)
                if not os.path.isdir(pd):
                    continue
                files = os.listdir(pd)
                jobs.append({'name': name, 'files': len(files),
                             'has_cover': 'cover.png' in files,
                             'has_content_html': 'content.html' in files})
            st['jobs'] = jobs
        runs = []
        for lp in (os.path.join(real, 'runs.log'), os.path.join(var_dir(), 'runs.log')):
            if os.path.exists(lp):
                with open(lp, errors='ignore') as f:
                    runs += [l.rstrip() for l in f.readlines()]
        st['recent_runs'] = runs[-8:]
        st['egress_ip'] = egress_ip()
        if st['config_ok'] and cfg:
            try:
                tok = get_token()
                if tok:
                    r = wx_call('/cgi-bin/draft/batchget', token=tok,
                                payload={'offset': 0, 'count': 1, 'no_content': 1})
                    st['draft_count'] = r.get('total_count')
            except Exception:
                pass
        return st

    def drafts(self):
        cfg = wx_config()
        tok = get_token()
        if not tok:
            return {'ok': False, 'error': '取 token 失败'}
        r = wx_call('/cgi-bin/draft/batchget', token=tok,
                    payload={'offset': 0, 'count': 20, 'no_content': 1})
        if r.get('errcode'):
            return {'ok': False, 'error': '微信返回 %s %s' % (r.get('errcode'), r.get('errmsg'))}
        items = []
        for it in r.get('item', []):
            news = (it.get('content') or {}).get('news_item') or [{}]
            items.append({'media_id': it.get('media_id'),
                          'title': news[0].get('title', ''),
                          'update_time': it.get('update_time')})
        return {'ok': True, 'total': r.get('total_count'), 'items': items}

    def do_GET(self):
        if '/api/v1' in self.path:
            return self.api_handle('GET')
        path = self._path()
        try:
            if path in ('/', '/index.html'):
                with open(os.path.join(WWW, 'index.html'), 'rb') as f:
                    return self._send(f.read())
            if path == '/settings':
                with open(os.path.join(WWW, 'settings.html'), 'rb') as f:
                    return self._send(f.read())
            if path == '/typeset':
                name = self._query().get('name', '')
                if not re.fullmatch(r'[A-Za-z0-9._-]+', name or ''):
                    return self._send(b'invalid name', code=400)
                return self._send(typeset_page(name).encode('utf-8'))
            if path == '/editor':
                with open(os.path.join(WWW, 'editor.html'), 'rb') as f:
                    return self._send(f.read())
            if path in ('/packages', '/generate', '/drafts', '/logs', '/api', '/about'):
                with open(os.path.join(WWW, path[1:] + '.html'), 'rb') as f:
                    return self._send(f.read())
            if path == '/about.json':
                un = os.uname()
                return self._json({'ok': True, 'version': APP_VERSION,
                                   'env': '%s · %s' % (un.sysname, un.machine),
                                   'data_dir': data_dir()})
            if path == '/state.json':
                return self._json(self.state())
            if path == '/drafts.json':
                return self._json(self.drafts())
            if path == '/settings-get':
                st = load_settings()
                return self._json({'ok': True,
                                   'wx_appid': st.get('wx_appid', ''),
                                   'wx_secret_set': bool(st.get('wx_secret')),
                                   'ds_key_set': bool(st.get('ds_key')),
                                   'ds_model': st.get('ds_model', 'deepseek-chat'),
                                   'author': st.get('author', ''),
                                   'label': st.get('label', ''),
                                   'intro': st.get('intro', ''),
                                   'api_token_set': bool(st.get('api_token'))})
            if path == '/spec-get':
                name = self._query().get('name', '')
                if not re.fullmatch(r'[A-Za-z0-9._-]+', name or ''):
                    return self._json({'ok': False, 'error': 'invalid name'}, 400)
                pdir = os.path.join(data_dir(), 'jobs', name)
                if not os.path.isdir(pdir):
                    return self._json({'ok': False, 'error': 'not found'}, 404)
                return self._json({'ok': True, 'name': name,
                                   'spec': package_spec(name, load_settings()),
                                   'files': sorted(os.listdir(pdir))})
            if path == '/pkgimg':
                q = self._query()
                name = q.get('name', '')
                fname = os.path.basename(q.get('f', ''))
                ext = os.path.splitext(fname)[1].lower()
                if not re.fullmatch(r'[A-Za-z0-9._-]+', name or '') or ext not in IMG_EXTS:
                    return self._send(b'invalid', code=400)
                fp = os.path.join(data_dir(), 'jobs', name, fname)
                if not os.path.exists(fp):
                    return self._send(b'not found', code=404)
                with open(fp, 'rb') as fh:
                    return self._send(fh.read(), IMG_EXTS[ext])
            if path == '/preview':
                name = self._query().get('name', '')
                if not re.fullmatch(r'[A-Za-z0-9._-]+', name or ''):
                    return self._send(b'invalid name', code=400)
                pdir = os.path.join(data_dir(), 'jobs', name)
                hp = os.path.join(pdir, 'content.html')
                if os.path.exists(hp):
                    with open(hp, 'rb') as f:
                        raw = f.read().decode('utf-8', 'ignore')
                    return self._send(rewrite_html_imgs(raw, name).encode('utf-8'))
                mp = os.path.join(pdir, 'article.md')
                if os.path.exists(mp):
                    meta, body = parse_front_matter(open(mp, encoding='utf-8').read())
                    page = ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
                            '<title>%s</title></head><body style="max-width:677px;margin:24px auto;'
                            'font-family:sans-serif;line-height:1.8;padding:0 16px">%s</body></html>'
                            % (meta.get('title', name), md_to_html(body)))
                    return self._send(rewrite_html_imgs(page, name).encode('utf-8'))
                return self._send(b'not found', code=404)
            if path == '/job':
                name = self._query().get('name', '')
                if not re.fullmatch(r'[A-Za-z0-9._-]+', name or ''):
                    return self._json({'ok': False, 'error': 'invalid name'}, 400)
                pdir = os.path.join(data_dir(), 'jobs', name)
                if not os.path.isdir(pdir):
                    return self._json({'ok': False, 'error': 'not found'}, 404)
                info = {'ok': True, 'name': name, 'files': sorted(os.listdir(pdir)),
                        'has_content_html': os.path.exists(os.path.join(pdir, 'content.html')),
                        'has_cover': os.path.exists(os.path.join(pdir, 'cover.png'))}
                mp = os.path.join(pdir, 'article.md')
                if os.path.exists(mp):
                    raw = open(mp, encoding='utf-8').read()
                    info['article_md'] = raw
                    meta, body = parse_front_matter(raw)
                    info['meta'] = meta
                    info['body'] = body
                return self._json(info)
            return self._send(b'not found', code=404)
        except Exception as e:
            return self._json({'ok': False, 'error': str(e)}, 500)

    def do_POST(self):
        if '/api/v1' in self.path:
            return self.api_handle('POST')
        path = self._path()
        try:
            data = self._read_json()
            if path == '/save-job':
                name = data.get('name', '')
                if not re.fullmatch(r'[A-Za-z0-9._-]+', name or ''):
                    return self._json({'ok': False, 'error': 'invalid name'}, 400)
                pdir = os.path.join(data_dir(), 'jobs', name)
                if not os.path.isdir(pdir):
                    return self._json({'ok': False, 'error': '包不存在'}, 404)
                cover = 'cover.png'
                mp0 = os.path.join(pdir, 'article.md')
                if os.path.exists(mp0):
                    m0, _ = parse_front_matter(open(mp0, encoding='utf-8').read())
                    cover = m0.get('cover') or cover
                body_md = data.get('md')
                if body_md is None:
                    body_md = ''
                    if os.path.exists(mp0):
                        body_md = parse_front_matter(open(mp0, encoding='utf-8').read())[1]
                fm = ('---\ntitle: %s\nauthor: %s\ndigest: %s\ncover: %s\n---\n\n%s\n'
                      % (data.get('title', name), load_settings().get('author', ''), data.get('digest', ''), cover,
                         body_md))
                with open(mp0, 'w', encoding='utf-8') as f:
                    f.write(fm)
                spf = os.path.join(pdir, 'spec.json')
                if os.path.exists(spf):
                    try:
                        spx = json.load(open(spf, encoding='utf-8'))
                        spx['title'] = data.get('title', name)
                        spx['summary'] = data.get('digest', '')
                        with open(spf, 'w', encoding='utf-8') as f:
                            json.dump(spx, f, ensure_ascii=False, indent=1)
                    except Exception:
                        pass
                return self._json({'ok': True})
            if path == '/drop-content':
                name = data.get('name', '')
                p = os.path.join(data_dir(), 'jobs', name, 'content.html')
                if os.path.exists(p):
                    os.remove(p)
                return self._json({'ok': True})
            if path == '/settings-get':
                st = load_settings()
                return self._json({'ok': True,
                                   'wx_appid': st.get('wx_appid', ''),
                                   'wx_secret_set': bool(st.get('wx_secret')),
                                   'ds_key_set': bool(st.get('ds_key')),
                                   'ds_model': st.get('ds_model', 'deepseek-chat'),
                                   'author': st.get('author', ''),
                                   'label': st.get('label', ''),
                                   'intro': st.get('intro', ''),
                                   'api_token_set': bool(st.get('api_token'))})
            if path == '/settings-save':
                st = load_settings()
                for k in ('wx_appid', 'ds_model', 'author', 'label', 'intro'):
                    if k in data and str(data[k]).strip() != '':
                        st[k] = str(data[k]).strip()
                if data.get('wx_secret'):
                    st['wx_secret'] = str(data['wx_secret']).strip()
                    try:
                        os.remove(os.path.join(var_dir(), 'token.json'))
                    except Exception:
                        pass
                if data.get('ds_key'):
                    st['ds_key'] = str(data['ds_key']).strip()
                save_settings(st)
                return self._json({'ok': True})
            if path == '/save-spec':
                name = data.get('name', '')
                spec = data.get('spec')
                if not re.fullmatch(r'[A-Za-z0-9._-]+', name or '') or not isinstance(spec, dict):
                    return self._json({'ok': False, 'error': 'invalid'}, 400)
                pdir = os.path.join(data_dir(), 'jobs', name)
                if not os.path.isdir(pdir):
                    return self._json({'ok': False, 'error': 'not found'}, 404)
                with open(os.path.join(pdir, 'spec.json'), 'w', encoding='utf-8') as f:
                    json.dump(spec, f, ensure_ascii=False, indent=1)
                log_run('save-spec ' + name)
                return self._json({'ok': True})
            if path == '/token-gen':
                return self.token_admin('gen')
            if path == '/token-revoke':
                return self.token_admin('revoke')
            if path == '/save-typeset':
                name = data.get('name', '')
                html = data.get('html', '')
                if not re.fullmatch(r'[A-Za-z0-9._-]+', name or '') or not html:
                    return self._json({'ok': False, 'error': 'invalid'}, 400)
                pdir = os.path.join(data_dir(), 'jobs', name)
                if not os.path.isdir(pdir):
                    return self._json({'ok': False, 'error': '包不存在'}, 404)
                with open(os.path.join(pdir, 'content.html'), 'w', encoding='utf-8') as f:
                    f.write(unrewrite_html_imgs(html))
                return self._json({'ok': True, 'bytes': len(html)})
            if path == '/ai-config':
                if 'key' in data:
                    ai = load_ai()
                    if data.get('key'):
                        ai['key'] = data['key'].strip()
                    if data.get('model'):
                        ai['model'] = data['model'].strip()
                    os.makedirs(var_dir(), exist_ok=True)
                    json.dump(ai, open(ai_path(), 'w'))
                    return self._json({'ok': True})
                ai = load_ai()
                return self._json({'ok': True, 'has_key': bool(ai.get('key')),
                                   'model': ai.get('model') or 'deepseek-chat'})
            if path == '/generate':
                topic = (data.get('topic') or '').strip()
                if not topic:
                    return self._json({'ok': False, 'error': '主题不能为空'}, 400)
                return self._json(do_generate(topic, (data.get('notes') or '').strip()))
            if path == '/default-cover':
                name = data.get('name', '')
                if not re.fullmatch(r'[A-Za-z0-9._-]+', name or ''):
                    return self._json({'ok': False, 'error': 'invalid name'}, 400)
                pdir = os.path.join(data_dir(), 'jobs', name)
                if not os.path.isdir(pdir):
                    return self._json({'ok': False, 'error': '包不存在'}, 404)
                default_cover(pdir)
                return self._json({'ok': True})
            if path == '/upload':
                name = data.get('name', '')
                if not re.fullmatch(r'[A-Za-z0-9._-]+', name or ''):
                    return self._json({'ok': False, 'error': 'invalid name'}, 400)
                return self._json(do_upload(name))
            if path == '/delete-draft':
                cfg = wx_config()
                tok = get_token()
                r = wx_call('/cgi-bin/draft/delete', token=tok,
                            payload={'media_id': data.get('media_id', '')})
                return self._json({'ok': r.get('errcode') == 0, 'wechat': r})
            if path == '/create-job':
                name = data.get('name', '')
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name or ''):
                    return self._json({'ok': False, 'error': '包名只能用字母数字 . _ -'}, 400)
                pdir = os.path.join(data_dir(), 'jobs', name)
                if os.path.exists(pdir):
                    return self._json({'ok': False, 'error': '包已存在'}, 400)
                os.makedirs(pdir)
                fm = ('---\ntitle: %s\nauthor: %s\ndigest: %s\ncover: cover.png\n---\n\n%s\n'
                      % (data.get('title', name), data.get('author') or load_settings().get('author', ''),
                         data.get('digest', ''), data.get('md', '')))
                with open(os.path.join(pdir, 'article.md'), 'w', encoding='utf-8') as f:
                    f.write(fm)
                return self._json({'ok': True, 'name': name})
            if path == '/job-file':
                name = data.get('name', '')
                fname = os.path.basename(data.get('filename', ''))
                if not re.fullmatch(r'[A-Za-z0-9._-]+', name or '') or not fname:
                    return self._json({'ok': False, 'error': 'invalid'}, 400)
                pdir = os.path.join(data_dir(), 'jobs', name)
                if not os.path.isdir(pdir):
                    return self._json({'ok': False, 'error': '包不存在'}, 404)
                raw = base64.b64decode(data.get('data_base64', ''))
                with open(os.path.join(pdir, fname), 'wb') as f:
                    f.write(raw)
                return self._json({'ok': True, 'filename': fname, 'bytes': len(raw)})
            if path == '/delete-job':
                name = data.get('name', '')
                if not re.fullmatch(r'[A-Za-z0-9._-]+', name or ''):
                    return self._json({'ok': False, 'error': 'invalid name'}, 400)
                pdir = os.path.join(data_dir(), 'jobs', name)
                if os.path.isdir(pdir):
                    shutil.rmtree(pdir)
                    return self._json({'ok': True})
                return self._json({'ok': False, 'error': 'not found'}, 404)
            return self._send(b'not found', code=404)
        except Exception as e:
            return self._json({'ok': False, 'error': str(e)}, 500)


    def do_DELETE(self):
        if '/api/v1' in self.path:
            return self.api_handle('DELETE')
        return self._send(b'not found', code=404)

    def api_handle(self, method):
        try:
            st = load_settings()
            tok = st.get('api_token') or ''
            if not tok:
                return self._json({'ok': False, 'error': 'API 未启用:先在设置页生成访问令牌'}, 403)
            provided = self.headers.get('X-Api-Token') or ''
            auth = self.headers.get('Authorization') or ''
            if auth.startswith('Bearer '):
                provided = auth[7:]
            if not provided or not hmac.compare_digest(provided, tok):
                return self._json({'ok': False, 'error': '令牌无效'}, 401)
            raw = self.path
            rest = raw[raw.index('/api/v1') + 7:]
            rest = rest.split('?')[0]
            segs = [urllib.parse.unquote(x) for x in rest.split('/') if x]
            data = {}
            if method in ('POST', 'DELETE'):
                try:
                    data = self._read_json()
                except Exception:
                    data = {}
            if method == 'GET' and segs == ['status']:
                info = self.state()
                return self._json({'ok': True, 'config_ok': info['config_ok'],
                                   'egress_ip': info['egress_ip'],
                                   'draft_count': info['draft_count'],
                                   'jobs': info['jobs']})
            if segs and segs[0] == 'packages':
                if method == 'GET' and len(segs) == 1:
                    return self._json({'ok': True, 'jobs': api_jobs()})
                if method == 'GET' and len(segs) == 2:
                    name = segs[1]
                    if not re.fullmatch(r'[A-Za-z0-9._-]+', name):
                        return self._json({'ok': False, 'error': 'invalid name'}, 400)
                    info = api_job_info(name)
                    if info is None:
                        return self._json({'ok': False, 'error': 'not found'}, 404)
                    return self._json(info)
                if method == 'POST' and len(segs) == 1:
                    name = (data.get('name') or '').strip()
                    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name):
                        return self._json({'ok': False, 'error': '包名只能用字母数字 . _ -'}, 400)
                    pdir = os.path.join(data_dir(), 'jobs', name)
                    if os.path.exists(pdir) and not data.get('overwrite'):
                        return self._json({'ok': False, 'error': '包已存在(如需覆盖传 overwrite=true)'}, 400)
                    os.makedirs(pdir, exist_ok=True)
                    fm = ('---\ntitle: %s\nauthor: %s\ndigest: %s\ncover: %s\n---\n\n%s\n'
                          % (data.get('title') or name,
                             data.get('author') or load_settings().get('author', ''),
                             data.get('digest', ''), data.get('cover') or 'cover.png',
                             data.get('md', '')))
                    with open(os.path.join(pdir, 'article.md'), 'w', encoding='utf-8') as f:
                        f.write(fm)
                    if data.get('spec') is not None:
                        with open(os.path.join(pdir, 'spec.json'), 'w', encoding='utf-8') as f:
                            json.dump(data['spec'], f, ensure_ascii=False, indent=1)
                    written = []
                    for item in data.get('files') or []:
                        fname = os.path.basename(item.get('filename', ''))
                        if not fname or fname in ('article.md', 'spec.json', 'content.html'):
                            continue
                        with open(os.path.join(pdir, fname), 'wb') as f:
                            f.write(base64.b64decode(item.get('data_base64', '')))
                        written.append(fname)
                    log_run('api 建包 ' + name)
                    return self._json({'ok': True, 'name': name, 'files_written': written})
                if len(segs) == 2 and method == 'DELETE':
                    name = segs[1]
                    if not re.fullmatch(r'[A-Za-z0-9._-]+', name):
                        return self._json({'ok': False, 'error': 'invalid name'}, 400)
                    pdir = os.path.join(data_dir(), 'jobs', name)
                    if os.path.isdir(pdir):
                        shutil.rmtree(pdir)
                        return self._json({'ok': True})
                    return self._json({'ok': False, 'error': 'not found'}, 404)
                if len(segs) == 3 and segs[2] == 'typeset' and method == 'POST':
                    name = segs[1]
                    if not re.fullmatch(r'[A-Za-z0-9._-]+', name):
                        return self._json({'ok': False, 'error': 'invalid name'}, 400)
                    obj, code = api_typeset(name, data.get('spec'))
                    return self._json(obj, code)
                if len(segs) == 3 and segs[2] == 'upload' and method == 'POST':
                    name = segs[1]
                    if not re.fullmatch(r'[A-Za-z0-9._-]+', name):
                        return self._json({'ok': False, 'error': 'invalid name'}, 400)
                    return self._json(do_upload(name))
            if segs and segs[0] == 'drafts':
                if method == 'GET' and len(segs) == 1:
                    return self._json(self.drafts())
                if method == 'POST' and len(segs) == 3 and segs[2] == 'delete':
                    cfg = wx_config()
                    tok2 = get_token()
                    if not tok2:
                        return self._json({'ok': False, 'error': '公众号配置不完整或白名单未加'}, 400)
                    r = wx_call('/cgi-bin/draft/delete', token=tok2,
                                payload={'media_id': segs[1]})
                    return self._json({'ok': r.get('errcode') == 0, 'wechat': r})
            if method == 'GET' and segs == ['logs']:
                runs = []
                for lp in (os.path.join(data_dir(), 'runs.log'),
                           os.path.join(var_dir(), 'runs.log')):
                    if os.path.exists(lp):
                        with open(lp, errors='ignore') as f:
                            runs += [l.rstrip() for l in f.readlines()]
                return self._json({'ok': True, 'runs': runs[-50:]})
            return self._json({'ok': False, 'error': 'unknown endpoint'}, 404)
        except Exception as e:
            return self._json({'ok': False, 'error': str(e)}, 500)

    def token_admin(self, action):
        st = load_settings()
        if action == 'gen':
            tok = secrets.token_urlsafe(24)
            st['api_token'] = tok
            save_settings(st)
            return self._json({'ok': True, 'token': tok})
        st.pop('api_token', None)
        save_settings(st)
        return self._json({'ok': True})


class Server(UnixStreamServer):
    daemon_threads = True


if __name__ == '__main__':
    if os.path.exists(SOCK):
        os.remove(SOCK)
    try:
        tcp = ThreadingHTTPServer(('0.0.0.0', API_PORT), Handler)
        tcp.daemon_threads = True
        threading.Thread(target=tcp.serve_forever, daemon=True).start()
    except Exception:
        pass
    Server(SOCK, Handler).serve_forever()
