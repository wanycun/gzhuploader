#!/usr/bin/env python3
"""v1.1.2 精简组件渲染引擎（单一来源）—— gzh-typeset。

只做 5 个样式组件 + 基础正文：
  1. 头部摘要区（刊头条：圆点 + 标签 + 细线 + 日期 / 大标题 / 摘要 / 主色短杠）
  2. 段落标题（编号 01/02…，10 套内置样式可选）
  3. 表格（flex 仿表格，浅底表头）
  4. 引用（主色左竖条 + 浅底）
  5. 尾部（作者签名 + 点赞/在看/转发三连 + THANKS FOR READING）

主题基因来自 gzh-typeset 三套原创主题（references/theme-index.md 权威值）：
  electric-blueprint 电光蓝图（默认，技术向）/ indigo-archive 靛蓝档案（分析向，衬线标题）
  / sand-magazine 暖砂杂志（叙事向，衬线标题、大圆角）

同一套模板同时服务两处：
  - render_article()   → 生成干净正文 HTML（spec.theme 或默认电光蓝图，供校验 + 粘贴兜底）
  - prepare_spec()     → 把文本预转换为内联 HTML，嵌入交互预览页由 JS 二次渲染

所有输出遵守平台红线：无 class/id/div、样式全内联、中文文本全部 <span leaf=""> 包裹、
正文标点自动全角化（代码内除外）。尾区三个线性图标用 SVG（校验器白名单放行，见
validate_gzh_html.py 的 FOOTER_SVG_*；其余 svg 一律 ERROR）。
"""

import re

# ---------- 设计变量（默认 = 电光蓝图，权威值见 references/theme-index.md） ----------

DEFAULT_STYLE = {
    "accent": "#0B63CE",   # 主色（电光蓝）
    "light": "#E4EEFB",    # 浅底（引用/表头/图标底）
    "bg": "#F5F8FC",       # 正文背景（冷白纸）
    "heading": "#0E1726",  # 标题色（深字）
    "text": "#3D4A5C",     # 正文色
    "sub": "#6E7C90",      # 次要文字
    "line": "#D5E1F0",     # 边框/分隔线
    "fs": 15,              # 正文字号 px
    "gap": 24,             # 区块间距 px
    "radius": 3,           # 圆角 px
    "font": "-apple-system,system-ui,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif",
    # 标题字体：电光蓝图与正文同栈；靛蓝档案/暖砂杂志为衬线体（主题 DNA，见各主题文件）
    "titleFont": "-apple-system,system-ui,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif",
}

_SERIF = "Georgia,'Songti SC','Noto Serif CJK SC','Source Han Serif SC',serif"

# 三套主题预设：spec.theme 指定干净正文主题；交互预览页 PRESETS 与此镜像（含 fs/gap/radius）。
# 色值必须与 references/theme-index.md 权威值表一致（lint_themes.py 会交叉核对）。
THEMES = {
    "electric-blueprint": dict(DEFAULT_STYLE),
    "indigo-archive": {
        "accent": "#2B3A67", "light": "#E6EAF5", "bg": "#F4F6FB",
        "heading": "#18203F", "text": "#464C66", "sub": "#6B7290", "line": "#C8D0E4",
        "fs": 15, "gap": 24, "radius": 4,
        "font": DEFAULT_STYLE["font"], "titleFont": _SERIF,
    },
    "sand-magazine": {
        "accent": "#A85C27", "light": "#F5E9D8", "bg": "#FDF8F1",
        "heading": "#3B2E24", "text": "#5C4C3C", "sub": "#7D6B58", "line": "#E3D5C3",
        "fs": 16, "gap": 26, "radius": 10,
        "font": DEFAULT_STYLE["font"], "titleFont": _SERIF,
    },
}

# 中文字体栈：按「正统 → 常用替补 → 大类兜底」排序，本机缺字体会自动降级，不影响排版。
# 顺序必须与 templates/interactive-template.html 的 FONTS 数组一致（面板按索引取值）。
# 第三位 = 移动端支持度备注。背景：公众号禁 @font-face 无法内嵌字体，发布后能否生效
# 取决于读者设备是否安装该字体——iOS 内置字体多（楷体/宋体/黑体齐全），
# 安卓 ROM 只有黑体系稳定，宋体部分机型有，楷体/仿宋/圆体基本没有 → 会降级为系统默认。
FONT_OPTIONS = [
    ("系统默认", "-apple-system,system-ui,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif",
     "双端稳定（跟随系统）"),
    ("现代黑体", "'PingFang SC','MiSans','HarmonyOS Sans SC','HONOR Sans','OPPO Sans','Microsoft YaHei',sans-serif",
     "双端稳定（已适配主流安卓 ROM）"),
    ("书卷宋体（小说常用）", "'Songti SC','Noto Serif SC','Source Han Serif SC','SimSun',serif",
     "iOS 稳定；安卓部分机型降级为默认"),
    ("手记楷体（霞鹜文楷）", "'LXGW WenKai','Kaiti SC','STKaiti','KaiTi',serif",
     "iOS 稳定；安卓多降级为默认"),
    ("清雅仿宋", "'FangSong','STFangsong','FangSong_GB2312',serif",
     "仅桌面；手机降级为默认"),
    ("圆润幼圆", "'Yuanti SC','Yuanti TC','YouYuan',sans-serif",
     "仅桌面；手机降级为默认"),
    ("等线硬朗", "'DengXian','Segoe UI','Source Han Sans SC',sans-serif",
     "仅桌面；手机降级为默认"),
    ("冬青雅黑", "'Hiragino Sans GB','Heiti SC','PingFang SC',sans-serif",
     "iOS 稳定；安卓降级为系统黑体"),
]

# 段落标题内置样式（索引必须与 interactive-template.html 的 HEADING_STYLES 一致）
HEADING_STYLES = ["编号·竖线（默认）", "色块·编号", "居中·短杠", "浅底·左条", "底线·编号",
                  "圆环·编号", "深底·白字", "粗左条", "居中·圆点", "框线·编号"]

# ---------- 行内标记与标点 ----------

_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_HL_RE = re.compile(r"==(.+?)==")
_PUNCT_MAP = {",": "，", ";": "；", "!": "！", "?": "？"}
# 中文在前 / 中文在后（任一侧邻接中文即转全角；"http://"、"12:30"、"1,000" 等无非汉字邻接，不动）
_PUNCT_AFTER_CJK_RE = re.compile(r"([一-鿿㐀-䶿])([,;!?])")
_PUNCT_BEFORE_CJK_RE = re.compile(r"([,;!?])(?=[一-鿿㐀-䶿])")
_COLON_RE = re.compile(r"([一-鿿㐀-䶿]):|:(?=[一-鿿㐀-䶿])")
_PAREN_RE = re.compile(r"\(([^()]*)\)")
_DOTS_RE = re.compile(r"([一-鿿㐀-䶿])\.{2,}")
# 句点：中文后、且不跟数字/字母才转全角——保护 "例子.com" 这类域名与 "v1.2" 版本号
_DOT_RE = re.compile(r"([一-鿿㐀-䶿])\.(?![0-9A-Za-z])")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _auto_punct(t):
    """中文语境邻接的半角标点转全角（代码 span 内不走此路径——先藏后转）。
    覆盖 , ; ! ? : 以及括号对 / 句点 / 省略号；英文直引号需人工（配对风险，不自动转）。"""
    t = _PUNCT_AFTER_CJK_RE.sub(lambda m: m.group(1) + _PUNCT_MAP[m.group(2)], t)
    t = _PUNCT_BEFORE_CJK_RE.sub(lambda m: _PUNCT_MAP[m.group(1)], t)
    t = _COLON_RE.sub(lambda m: (m.group(1) or "") + "：", t)

    def _paren(m):
        inner = m.group(1)
        prev = t[m.start() - 1] if m.start() > 0 else ""
        if _CJK_RE.search(inner) or _CJK_RE.match(prev):
            return "（%s）" % inner
        return m.group(0)

    t = _PAREN_RE.sub(_paren, t)
    t = _DOTS_RE.sub(r"\1……", t)
    t = _DOT_RE.sub(r"\1。", t)
    return t


def inline(text, S, token=False):
    """纯文本 → 行内 HTML。支持 **加粗** ==高亮== `代码`。
    整体包一层 <span leaf="">，样式子 span 置于其内；
    <strong> 内的文字同样包 leaf（gzh-typeset 校验器要求，否则粘贴丢色）。

    token=True 时高亮框的圆角写成 §R§ 占位符，供预览页 JS 按当前圆角实时替换
    （prepare_spec 用）；生成干净正文时 token=False，直接写入具体数值。
    """
    radius = "§R§" if token else S["radius"]
    s = esc(text)
    codes = []

    def _stash(m):
        codes.append(m.group(1))
        return "\x00%d\x00" % (len(codes) - 1)

    s = _CODE_RE.sub(_stash, s)
    s = _auto_punct(s)
    s = _BOLD_RE.sub(
        r'<strong style="color:%s;"><span leaf="">\1</span></strong>' % S["heading"], s)
    s = _HL_RE.sub(
        r'<span style="background:%s;padding:1px 5px;border-radius:%spx;'
        r'font-weight:600;color:%s;"><span leaf="">\1</span></span>' % (S["light"], radius, S["heading"]), s)
    for i, c in enumerate(codes):
        s = s.replace("\x00%d\x00" % i,
                      '<span style="background:%s;color:%s;padding:1px 6px;'
                      'border-radius:%spx;font-family:ui-monospace,Menlo,Consolas,monospace;'
                      'font-size:%spx;">%s</span>'
                      % (S["light"], S["heading"], radius, S["fs"] - 1, c))
    return '<span leaf="">' + s + "</span>"


def title_html(text, S):
    """标题行内 HTML：**强调词** → 主色下划线。"""
    s = esc(text)
    s = _auto_punct(s)
    parts = re.split(r"\*\*(.+?)\*\*", s)
    out = []
    for i, p in enumerate(parts):
        if not p:
            continue
        if i % 2 == 1:
            out.append('<span style="border-bottom:3px solid %s;">'
                       '<span leaf="">%s</span></span>' % (S["accent"], p))
        else:
            out.append('<span leaf="">%s</span>' % p)
    return "".join(out)


# ---------- 区块模板（Python 侧，JS 侧在 interactive-template.html 保持镜像） ----------

def _spacer(S):
    """彩色隔距层：块与块之间的间距节点。公众号会剥掉块的 padding（background-color 保留、内距归零），
    单靠彩色 padding-top 撑距时，块间会露出白色默认间距。改为插入一个与块同深度、带
    background-color 的 section，用 line-height 撑出高度——间隙本身是彩色节点，深度够即穿透清洗。"""
    return ('<section style="margin:0;background-color:%s;line-height:%spx;'
            'font-size:12px;"><span leaf="">&nbsp;</span></section>'
            % (S["bg"], S["gap"]))


def _blk(inner, S, first, bottom=False):
    """块级包装：底色下沉到每个块自身（微信会剥剪贴板最外层节点的 padding，只保留 background-color）。
    块间距不能靠会被剥掉的 padding/margin——由 `_spacer` 彩色隔距层提供（首块/页首除外）。
    块带横向 16px 内距 + 可选底部 16px（末块收边），margin:0 归零公众号平铺默认 margin。"""
    pb = "16px" if bottom else "0"
    pre = "" if first else _spacer(S)
    return pre + ('<section style="margin:0;padding:0 16px %s;background-color:%s;">%s</section>'
                  % (pb, S["bg"], inner))


def b_header(meta, S):
    label = meta.get("label") or "橄榄手记"
    date = meta.get("date") or ""
    bar = ('<section style="display:flex;align-items:center;gap:8px;padding-bottom:20px;">'
           '<span style="width:8px;height:8px;background:%s;border-radius:50%%;'
           'display:inline-block;overflow:hidden;vertical-align:middle;font-size:0;line-height:0;">'
           '<span leaf="">&nbsp;</span></span>'
           '<span style="font-size:11px;font-weight:700;letter-spacing:3px;color:%s;">'
           '<span leaf="">%s</span></span>'
           '<span style="flex:1;height:1px;background:%s;display:inline-block;overflow:hidden;'
           'vertical-align:middle;font-size:0;line-height:0;"><span leaf="">&nbsp;</span></span>'
           '<span style="font-size:11px;color:%s;font-weight:500;">'
           '<span leaf="">%s</span></span></section>'
           % (S["accent"], S["sub"], esc(label), S["line"], S["sub"], esc(date)))
    title = ('<p style="font-size:23px;font-weight:800;color:%s;margin:0;padding:0 0 12px;'
             'line-height:1.35;font-family:%s;">%s</p>'
             % (S["heading"], S.get("titleFont") or S["font"],
                title_html(meta.get("title", ""), S)))
    summary = ""
    if meta.get("summary"):
        summary = ('<p style="font-size:14px;color:%s;margin:0;padding:0 0 16px;line-height:1.8;">'
                   '<span leaf="">%s</span></p>'
                   % (S["sub"], inline(meta["summary"], S)))
    dash = ('<section style="width:42px;height:3px;background:%s;border-radius:2px;">'
            '<span leaf="">&nbsp;</span></section>' % S["accent"])
    inner = ('<section style="padding:6px 0 2px;">%s%s%s%s</section>'
             '<section style="border-bottom:1px solid %s;padding-top:18px;">'
             '<span leaf="">&nbsp;</span></section>'
             % (bar, title, summary, dash, S["line"]))
    return _blk(inner, S, True)


def _mini_dash(S, w=18, h=2):
    """标题装饰短杠（居中式标题两侧用）。"""
    return ('<span style="width:%dpx;height:%dpx;background:%s;display:inline-block;'
            'overflow:hidden;vertical-align:middle;font-size:0;line-height:0;">'
            '<span leaf="">&nbsp;</span></span>' % (w, h, S["accent"]))


def b_heading(num, text, S):
    """段落标题，按 S['hstyle'] 分派 10 套内置样式（与 interactive-template.html 镜像）。
    外覆一层 titleFont section：靛蓝档案/暖砂杂志的标题为衬线体（主题 DNA）。"""
    hs = int(S.get("hstyle", 0) or 0)
    if hs == 1:   # 色块·编号：主色方块白字编号 + 标题
        inner = ('<section style="display:flex;align-items:center;gap:10px;">'
                 '<span style="width:32px;height:32px;background:%s;border-radius:%spx;'
                 'display:flex;align-items:center;justify-content:center;color:#ffffff;'
                 'font-size:14px;font-weight:800;letter-spacing:1px;flex-shrink:0;'
                 'line-height:1;"><span leaf="">%s</span></span>'
                 '<span style="font-size:17px;font-weight:700;color:%s;">'
                 '<span leaf="">%s</span></span></section>'
                 % (S["accent"], S["radius"], num, S["heading"], inline(text, S)))
    elif hs == 2:  # 居中·短杠：两侧主色短杠 + 编号 + 标题，整体居中
        inner = ('<section style="display:flex;align-items:center;justify-content:center;gap:10px;">'
                 '%s'
                 '<span style="font-size:12px;font-weight:700;color:%s;letter-spacing:1px;">'
                 '<span leaf="">%s</span></span>'
                 '<span style="font-size:16px;font-weight:700;color:%s;">'
                 '<span leaf="">%s</span></span>'
                 '%s</section>'
                 % (_mini_dash(S), S["accent"], num, S["heading"],
                    inline(text, S), _mini_dash(S)))
    elif hs == 3:  # 浅底·左条：浅底圆角标题条 + 主色左竖条
        inner = ('<section style="background:%s;border-left:4px solid %s;'
                 'border-radius:%spx;padding:10px 14px;">'
                 '<section style="display:flex;align-items:center;gap:10px;">'
                 '<span style="font-size:14px;font-weight:800;color:%s;line-height:1;">'
                 '<span leaf="">%s</span></span>'
                 '<span style="font-size:16px;font-weight:700;color:%s;">'
                 '<span leaf="">%s</span></span>'
                 '</section></section>'
                 % (S["light"], S["accent"], S["radius"],
                    S["accent"], num, S["heading"], inline(text, S)))
    elif hs == 4:  # 底线·编号：编号 + 标题，底部通栏主色细线
        inner = ('<section style="display:flex;align-items:baseline;gap:10px;'
                 'border-bottom:2px solid %s;padding-bottom:8px;">'
                 '<span style="font-size:18px;font-weight:800;color:%s;line-height:1;">'
                 '<span leaf="">%s</span></span>'
                 '<span style="font-size:16px;font-weight:700;color:%s;">'
                 '<span leaf="">%s</span></span></section>'
                 % (S["accent"], S["accent"], num, S["heading"], inline(text, S)))
    elif hs == 5:  # 圆环·编号：描边圆圈内嵌编号 + 标题
        inner = ('<section style="display:flex;align-items:center;gap:10px;">'
                 '<span style="width:30px;height:30px;border:2px solid %s;border-radius:50%%;'
                 'display:flex;align-items:center;justify-content:center;color:%s;'
                 'font-size:13px;font-weight:800;flex-shrink:0;line-height:1;">'
                 '<span leaf="">%s</span></span>'
                 '<span style="font-size:17px;font-weight:700;color:%s;">'
                 '<span leaf="">%s</span></span></section>'
                 % (S["accent"], S["accent"], num, S["heading"], inline(text, S)))
    elif hs == 6:  # 深底·白字：标题色通栏圆角条 + 白字标题 + 主色编号（强调词深色一并翻白）
        inner = ('<section style="background:%s;border-radius:%spx;padding:9px 14px;'
                 'display:flex;align-items:center;gap:10px;">'
                 '<span style="font-size:13px;font-weight:800;color:%s;line-height:1;'
                 'letter-spacing:1px;"><span leaf="">%s</span></span>'
                 '<span style="font-size:15px;font-weight:700;color:#ffffff;">%s</span></section>'
                 % (S["heading"], S["radius"], S["accent"], num,
                    inline(text, dict(S, heading="#ffffff"))))
    elif hs == 7:  # 粗左条：主色粗竖条 + 编号 + 标题
        inner = ('<section style="border-left:5px solid %s;padding-left:12px;'
                 'display:flex;align-items:baseline;gap:10px;">'
                 '<span style="font-size:18px;font-weight:800;color:%s;line-height:1;">'
                 '<span leaf="">%s</span></span>'
                 '<span style="font-size:16px;font-weight:700;color:%s;">'
                 '<span leaf="">%s</span></span></section>'
                 % (S["accent"], S["accent"], num, S["heading"], inline(text, S)))
    elif hs == 8:  # 居中·圆点：两侧主色圆点 + 居中标题
        dot = ('<span style="width:6px;height:6px;background:%s;border-radius:50%%;'
               'display:inline-block;overflow:hidden;vertical-align:middle;font-size:0;'
               'line-height:0;"><span leaf="">&nbsp;</span></span>' % S["accent"])
        inner = ('<section style="display:flex;align-items:center;justify-content:center;gap:10px;">'
                 '%s'
                 '<span style="font-size:12px;font-weight:700;color:%s;letter-spacing:1px;">'
                 '<span leaf="">%s</span></span>'
                 '<span style="font-size:16px;font-weight:700;color:%s;">'
                 '<span leaf="">%s</span></span>'
                 '%s</section>'
                 % (dot, S["accent"], num, S["heading"], inline(text, S), dot))
    elif hs == 9:  # 框线·编号：细边框圆角框 + 编号 + 小竖线 + 标题
        inner = ('<section style="border:1px solid %s;border-radius:%spx;padding:8px 14px;'
                 'display:flex;align-items:center;gap:10px;">'
                 '<span style="font-size:16px;font-weight:800;color:%s;line-height:1;">'
                 '<span leaf="">%s</span></span>'
                 '<span style="width:1px;height:14px;background:%s;display:inline-block;'
                 'overflow:hidden;vertical-align:middle;font-size:0;line-height:0;">'
                 '<span leaf="">&nbsp;</span></span>'
                 '<span style="font-size:16px;font-weight:700;color:%s;">'
                 '<span leaf="">%s</span></span></section>'
                 % (S["line"], S["radius"], S["accent"], num, S["line"],
                    S["heading"], inline(text, S)))
    else:          # 编号·竖线（默认，刊头条基因）
        inner = ('<section style="display:flex;align-items:center;gap:12px;">'
                 '<span style="font-size:20px;font-weight:800;color:%s;line-height:1;'
                 'letter-spacing:1px;"><span leaf="">%s</span></span>'
                 '<span style="width:1px;height:18px;background:%s;display:inline-block;'
                 'overflow:hidden;vertical-align:middle;font-size:0;line-height:0;">'
                 '<span leaf="">&nbsp;</span></span>'
                 '<span style="font-size:17px;font-weight:700;color:%s;">'
                 '<span leaf="">%s</span></span></section>'
                 % (S["accent"], num, S["line"], S["heading"], inline(text, S)))
    wrapped = ('<section style="font-family:%s;">%s</section>'
               % (S.get("titleFont") or S["font"], inner))
    return _blk(wrapped, S, False)


def b_para(text, S, first=False):
    inner = ('<p style="margin:0;font-size:%spx;line-height:1.9;text-align:justify;'
             'color:%s;">%s</p>' % (S["fs"], S["text"], inline(text, S)))
    return _blk(inner, S, first)


def b_quote(text, S, first=False, source=""):
    src = ""
    if source:
        src = ('<p style="margin:0;padding:8px 0 0;font-size:12px;color:%s;text-align:right;">'
               '<span leaf="">—— %s</span></p>' % (S["sub"], esc(_auto_punct(source))))
    inner = ('<section style="background:%s;border-left:3px solid %s;'
             'border-radius:%spx;padding:14px 16px;">'
             '<p style="margin:0;font-size:%spx;line-height:1.85;color:%s;">%s</p>%s</section>'
             % (S["light"], S["accent"], S["radius"],
                S["fs"], S["text"], inline(text, S), src))
    return _blk(inner, S, first)


def b_table(head, rows, S, first=False):
    n = max(len(head), max((len(r) for r in rows), default=0), 2)
    w0 = 28 if n <= 3 else 22
    w_rest = (100 - w0) / (n - 1)

    def cell(v, bold, left_border):
        bd = 'border-left:1px solid %s;' % S["line"] if left_border else ""
        return ('<section style="width:%.4f%%;padding:9px 10px;box-sizing:border-box;'
                'font-size:%spx;%scolor:%s;">%s</section>'
                % (w_rest, S["fs"] - 1, bd, S["heading"] if bold else S["text"],
                   inline(v, S)))

    html_rows = []
    hd = '<section style="display:flex;background:%s;">' % S["light"]
    hd += ('<section style="width:%d%%;padding:9px 10px;box-sizing:border-box;'
           'font-size:%spx;font-weight:700;color:%s;"><span leaf="">%s</span></section>'
           % (w0, S["fs"] - 1, S["heading"], esc(_auto_punct(head[0] if head else ""))))
    for j, v in enumerate(head[1:n]):
        hd += ('<section style="width:%.4f%%;padding:9px 10px;box-sizing:border-box;'
               'border-left:1px solid %s;font-size:%spx;font-weight:700;color:%s;">'
               '<span leaf="">%s</span></section>'
               % (w_rest, S["line"], S["fs"] - 1, S["heading"], esc(_auto_punct(v))))
    hd += "</section>"
    html_rows.append(hd)
    for i, row in enumerate(rows):
        row = list(row) + [""] * (n - len(row))
        last = i == len(rows) - 1
        bd = "" if last else "border-bottom:1px solid %s;" % S["line"]
        r = '<section style="display:flex;%s">' % bd
        r += ('<section style="width:%d%%;padding:9px 10px;box-sizing:border-box;'
              'font-size:%spx;font-weight:600;color:%s;"><span leaf="">%s</span></section>'
              % (w0, S["fs"] - 1, S["heading"], _auto_punct(esc(row[0]))))
        for v in row[1:n]:
            r += cell(v, False, True)
        r += "</section>"
        html_rows.append(r)
    inner = ('<section style="border:1px solid %s;border-radius:%spx;overflow:hidden;'
             'background:%s;">%s</section>'
             % (S["line"], S["radius"], S["bg"], "".join(html_rows)))
    return _blk(inner, S, first)


def b_list(items, S, first=False):
    """假列表：公众号编辑器会清洗 <ul>/<li>（剥外层背景/搅乱列表结构，实测列表块边上露白），
    改用 <p> + 「·」前缀模拟列表项——p+span 是实测在公众号渲染最稳的结构。"""
    ps = "".join(
        '<p style="margin:0;padding:0 0 6px;font-size:%spx;line-height:1.8;color:%s;">'
        '<span leaf="">·&nbsp;%s</span></p>'
        % (S["fs"], S["text"], inline(it, S)) for it in items)
    return _blk(ps, S, first)


def b_img(src, S, first=False, cap=""):
    img = ('<span leaf=""><img src="%s" alt="图片" style="max-width:100%%;height:auto;'
           'display:block;margin:0 auto;border-radius:%spx;"></span>'
           % (esc(src), S["radius"]))
    caption = ""
    if cap:
        caption = ('<p style="margin:0;padding:8px 0 0;font-size:12px;color:%s;text-align:center;">'
                   '<span leaf="">%s</span></p>' % (S["sub"], esc(_auto_punct(cap))))
    return _blk(img + caption, S, first)


def b_footer(author, intro, outro, S):
    """尾部（统一样式与文案，与 byai-wechat-gzh-helper 在售版本同款）：
    第一行粗体欢迎语 → 第二行三连引导 → 第三行支持语 → 点赞/在看/转发三个圆形线性图标钮 →
    THANKS FOR READING。spec 的 author/intro/outro 仍可覆盖文案；不填则用统一默认。
    图标用 SVG 线性图标（stroke 风格，随主题色联动）；校验器仅白名单放行这三个 svg。"""
    author = author or ""
    intro = intro or "感谢阅读，我是 AiSeeker，关注我，一起来 AI 探索"
    outro = outro or "如文章对您有帮助，欢迎点赞、在看、转发三连"
    support = "您的支持是我的动力"

    def _icon(svg, label):
        return ('<section style="text-align:center;color:%s;">'
                '<section style="width:42px;height:42px;display:flex;align-items:center;'
                'justify-content:center;margin:0 auto;background:%s;border:1px solid %s;'
                'border-radius:50%%;">%s</section>'
                '<span style="font-size:11px;font-weight:600;display:block;padding-top:6px;">'
                '<span leaf="">%s</span></span></section>'
                % (S["text"], S["light"], S["line"], svg, label))

    # 尾区三个线性图标（与 byai-wechat-gzh-helper 在售版本同款，stroke 风格随主题色联动）。
    # 校验器仅白名单放行这三个 svg（validate_gzh_html.py FOOTER_SVG_*），其余 svg 仍 ERROR。
    svg_like = ('<svg width="19" height="19" viewBox="0 0 24 24" fill="none" '
                'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
                'stroke-linejoin="round" aria-hidden="true">'
                '<path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9'
                'a2 2 0 0 0-2-2.3zM7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"></path></svg>')
    svg_eye = ('<svg width="19" height="19" viewBox="0 0 24 24" fill="none" '
               'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
               'stroke-linejoin="round" aria-hidden="true">'
               '<circle cx="12" cy="12" r="3"></circle>'
               '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path></svg>')
    svg_share = ('<svg width="19" height="19" viewBox="0 0 24 24" fill="none" '
                 'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
                 'stroke-linejoin="round" aria-hidden="true">'
                 '<path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"></path>'
                 '<polyline points="16 6 12 2 8 6"></polyline>'
                 '<line x1="12" y1="2" x2="12" y2="15"></line></svg>')

    icons = ('<section style="display:flex;justify-content:center;gap:24px;">'
             + _icon(svg_like, "点赞") + _icon(svg_eye, "在看") + _icon(svg_share, "转发")
             + "</section>")
    author_line = ("我是 %s，%s" % (author, intro)) if author else intro
    inner = ('<section style="border-top:1px solid %s;padding-top:22px;text-align:center;">'
             '<p style="margin:0;padding:0 0 6px;font-size:13px;font-weight:700;color:%s;">'
             '<span leaf="">%s</span></p>'
             '<p style="margin:0;padding:0 0 4px;font-size:12px;color:%s;"><span leaf="">%s</span></p>'
             '<p style="margin:0;padding:0 0 18px;font-size:12px;color:%s;"><span leaf="">%s</span></p>'
             '%s'
             '<p style="margin:0;padding:18px 0 0;font-size:10px;color:%s;letter-spacing:2px;">'
             '<span leaf="">THANKS FOR READING</span></p></section>'
             % (S["line"], S["heading"], esc(_auto_punct(author_line)),
                S["sub"], esc(_auto_punct(outro)),
                S["sub"], esc(_auto_punct(support)), icons, S["sub"]))
    return _blk(inner, S, False, bottom=True)


# ---------- 整篇装配 ----------

def render_article(spec, S=None):
    """spec → 干净正文 HTML（纯 <section> 片段）。"""
    S = dict(DEFAULT_STYLE, **(S or {}))
    parts = [b_header(spec, S)]
    num = 0
    for sec in spec.get("sections", []):
        blocks = sec.get("blocks", [])
        if sec.get("heading", ""):
            num += 1
            parts.append(b_heading("%02d" % num, sec["heading"], S))
        for j, b in enumerate(blocks):
            t = b.get("t")
            if t == "p":
                parts.append(b_para(b.get("x", ""), S))
            elif t == "quote":
                parts.append(b_quote(b.get("x", ""), S, source=b.get("from", "")))
            elif t == "table":
                parts.append(b_table(b.get("head", []), b.get("rows", []), S))
            elif t == "list":
                parts.append(b_list(b.get("items", []), S))
            elif t == "img":
                parts.append(b_img(b.get("src", ""), S, cap=b.get("cap", "")))
    parts.append(b_footer(spec.get("author"), spec.get("intro"),
                          spec.get("outro"), S))
    parts.append('<p style="display:none;">'
                 '<mp-style-type data-value="3"></mp-style-type></p>')
    # 五层嵌套底色（实测微信编辑器粘贴时剥掉剪贴板最外层 1~4 层节点的样式，第 5 层起存活）：
    # L1~L4 是牺牲层（全带同色 background-color 兜底），L5 承载 padding/字体颜色；
    # 每个块包装也自带同色底（双保险，实测块底色可穿透）。
    # 用 background-color 长写而非 background 简写（微信清洗白名单更认长写）。
    return ('<section style="max-width:677px;margin:0 auto;box-sizing:border-box;'
            'background-color:%s;">'
            '<section style="background-color:%s;">'
            '<section style="background-color:%s;">'
            '<section style="background-color:%s;">'
            '<section style="padding:16px;box-sizing:border-box;background-color:%s;'
            'color:%s;font-family:%s;line-height:1.75;">%s</section>'
            '</section></section></section></section>'
            % (S["bg"], S["bg"], S["bg"], S["bg"], S["bg"], S["text"], S["font"], "".join(parts)))


def prepare_spec(spec):
    """spec → 预转换版（文本已变 HTML），供交互预览页 JS 二次渲染使用。"""
    out = {
        "theme": spec.get("theme") or "electric-blueprint",
        "label": esc(spec.get("label") or "橄榄手记"),
        "date": esc(spec.get("date") or ""),
        "title": esc(spec.get("title") or ""),   # 保留 **强调词** 标记，由 JS 按主题色渲染
        "summary": inline(spec.get("summary") or "", DEFAULT_STYLE, token=True),
        "sections": [],
        "author": esc(_auto_punct(spec.get("author") or "")),
        "intro": esc(_auto_punct(spec.get("intro") or "感谢阅读，我是 AiSeeker，关注我，一起来 AI 探索")),
        "outro": esc(_auto_punct(spec.get("outro") or "如文章对您有帮助，欢迎点赞、在看、转发三连")),
    }
    for sec in spec.get("sections", []):
        blocks = []
        for b in sec.get("blocks", []):
            t = b.get("t")
            if t == "p":
                blocks.append({"t": "p", "h": inline(b.get("x", ""), DEFAULT_STYLE, token=True)})
            elif t == "quote":
                blocks.append({"t": "quote",
                               "h": inline(b.get("x", ""), DEFAULT_STYLE, token=True),
                               "from": esc(_auto_punct(b.get("from", "")))})
            elif t == "table":
                blocks.append({"t": "table",
                               "head": [esc(_auto_punct(v)) for v in b.get("head", [])],
                               "rows": [[esc(_auto_punct(v)) for v in r] for r in b.get("rows", [])]})
            elif t == "list":
                blocks.append({"t": "list",
                               "items": [inline(it, DEFAULT_STYLE, token=True)
                                         for it in b.get("items", [])]})
            elif t == "img":
                blocks.append({"t": "img", "src": b.get("src", ""),
                               "cap": esc(_auto_punct(b.get("cap", "") or ""))})
        out["sections"].append({"heading": inline(sec.get("heading", ""), DEFAULT_STYLE, token=True),
                                "blocks": blocks})
    return out
