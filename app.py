# app.py — 左侧目录树（类似资源管理器），右侧按需分析当前目录的子项大小
# 分析结果缓存到同级 folder_sizes.db，切走再切回可回看；「递归分析」会把整棵子树逐层入库
import ctypes
import json
import os
import queue
import re
import shutil
import sqlite3
import stat
import string
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from concurrent.futures import ThreadPoolExecutor
from tkinter import messagebox, ttk


LOG_Q = queue.Queue()  # 日志队列：工作线程 put，主线程 _poll 取出显示（tk 非线程安全）

_DPI_AWARE = False  # 是否已声明 DPI 感知：感知时 winfo 坐标是物理像素，未感知时是虚拟 96 DPI（=逻辑像素）


def enable_dpi_awareness():
    """必须在创建任何窗口前调用。Windows 百分比缩放下未感知的 Tk 按 96 DPI 渲染再被位图拉伸，
    文字发糊；声明感知后按真实 DPI 清晰渲染（Tk 8.6 字号按系统 DPI 自动换算）"""
    global _DPI_AWARE
    if sys.platform != 'win32':
        return
    try:
        if ctypes.windll.shcore.SetProcessDpiAwarenessContext(-4):  # PER_MONITOR_AWARE_V2
            _DPI_AWARE = True
            return
    except Exception:
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2):  # PROCESS_PER_MONITOR_DPI_AWARE
            _DPI_AWARE = True
            return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()  # 系统级感知，全版本兜底
        _DPI_AWARE = True
    except Exception:
        pass


def dpi_scale(hwnd):
    """窗口所在显示器的缩放比（1.0 = 100%）"""
    try:
        u32 = ctypes.windll.user32
        top = u32.GetParent(hwnd)
        return u32.GetDpiForWindow(top or hwnd) / 96.0
    except Exception:
        return 1.0


def log(msg):
    line = f'[{time.strftime("%H:%M:%S")}] {msg}'
    print(line, flush=True)
    LOG_Q.put(line)  # 同步进右侧窗口日志模块


def fmt_size(n):
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or u == 'TB':
            return f'{n} B' if u == 'B' else f'{n:.1f} {u}'
        n /= 1024


def safe(s):
    """含孤代理字符的文件名会让 Tcl 抛 UnicodeEncodeError，显示前清洗"""
    return s.encode('utf-8', errors='replace').decode('utf-8')


def dir_size(path):
    # 迭代式 scandir：DirEntry.stat 直接复用目录枚举返回的数据，
    # 不像 os.walk + getsize 那样对每个文件再发一次系统调用（被杀毒软件拦截时极慢）
    total, n = 0, 0
    stack = [path]
    while stack:
        try:
            it = os.scandir(stack.pop())
        except OSError:
            continue  # ponytail: 无权限的目录直接跳过
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    else:
                        total += e.stat(follow_symlinks=False).st_size
                        n += 1
                        if n % 50000 == 0:
                            log(f'    … {safe(path)} 已统计 {n} 个文件')
                except OSError:
                    pass
    return total


def scan(path):
    """返回 [(名称, 字节, 类型)]，按大小降序"""
    t0 = time.time()
    log(f'开始扫描: {safe(path)}')
    items = []
    try:
        entries = list(os.scandir(path))
    except OSError as e:
        log(f'无法列出目录: {e!r}')
        return items
    dirs, files = [], []
    for e in entries:
        try:
            (dirs if e.is_dir(follow_symlinks=False) else files).append(e)
        except OSError:
            pass
    log(f'共 {len(dirs)} 个文件夹 + {len(files)} 个文件，并行统计中')

    def work(e):
        t = time.time()
        size = dir_size(e.path)
        log(f'  {safe(e.name)} = {fmt_size(size)}，耗时 {time.time() - t:.1f}s')
        return (e.name, size, '文件夹')

    with ThreadPoolExecutor(max_workers=6) as pool:  # 完成顺序不定，日志不分先后
        items.extend(pool.map(work, dirs))
    for e in files:
        try:
            items.append((e.name, e.stat().st_size, '文件'))
        except OSError:
            pass
    items.sort(key=lambda x: x[1], reverse=True)
    log(f'扫描完成: {safe(path)}，总耗时 {time.time() - t0:.1f}s')
    return items


def walk_dir(D, out):
    """把 D 的直接子项记入 out（文件夹 size=递归总量），返回 D 总大小；沿途每层子目录都记录"""
    total, children = 0, []
    try:
        entries = list(os.scandir(D))
    except OSError:
        out[canon(D)] = []
        return 0
    for e in entries:
        try:
            if e.is_dir(follow_symlinks=False):
                sub = walk_dir(e.path, out)
                children.append((e.name, sub, '文件夹'))
                total += sub
            else:
                s = e.stat(follow_symlinks=False).st_size
                children.append((e.name, s, '文件'))
                total += s
        except OSError:
            pass
    children.sort(key=lambda x: x[1], reverse=True)
    out[canon(D)] = children
    return total


def recursive_scan(root):
    """scan 的递归版：返回 ({路径: [(名称, 字节, 类型)]}, root 总大小)，root 下每层目录都有记录"""
    t0 = time.time()
    log(f'开始递归扫描: {safe(root)}')
    by_parent = {}
    try:
        entries = list(os.scandir(root))
    except OSError as e:
        log(f'无法列出目录: {e!r}')
        return {canon(root): []}, 0
    dirs, files = [], []
    for e in entries:
        try:
            (dirs if e.is_dir(follow_symlinks=False) else files).append(e)
        except OSError:
            pass
    log(f'共 {len(dirs)} 个文件夹 + {len(files)} 个文件，并行递归统计中')

    def work(e):
        # 顶层文件夹子树互不相交，各线程写共享 dict 的键不冲突
        return e.name, walk_dir(e.path, by_parent), '文件夹'

    with ThreadPoolExecutor(max_workers=6) as pool:
        items = list(pool.map(work, dirs))
    for e in files:
        try:
            items.append((e.name, e.stat(follow_symlinks=False).st_size, '文件'))
        except OSError:
            pass
    items.sort(key=lambda x: x[1], reverse=True)
    total = sum(s for _, s, _ in items)
    by_parent[canon(root)] = items
    log(f'递归扫描完成: {safe(root)}，共 {len(by_parent)} 个目录，总耗时 {time.time() - t0:.1f}s')
    return by_parent, total


def subdirs(path):
    try:
        return sorted((e for e in os.scandir(path) if e.is_dir(follow_symlinks=False)),
                      key=lambda e: e.name.lower())
    except OSError:
        return []


def has_subdirs(path):
    try:
        return any(e.is_dir(follow_symlinks=False) for e in os.scandir(path))
    except OSError:
        return False


# ---- sqlite 持久化：分析过的目录缓存在 app.py 同级 folder_sizes.db，切回可回看 ----
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'folder_sizes.db')


def canon(p):
    """路径键统一：绝对+normpath，Windows 下转小写（大小写不敏感文件系统）"""
    p = os.path.normpath(os.path.abspath(p))
    return p.lower() if os.name == 'nt' else p


class Store:
    def __init__(self):
        self._lock = threading.Lock()  # 扫描线程写、主线程读
        self._conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._conn.execute(
            'CREATE TABLE IF NOT EXISTS item (parent TEXT NOT NULL, name TEXT NOT NULL, '
            'size INTEGER NOT NULL, type TEXT NOT NULL, scanned_at TEXT NOT NULL, '
            'PRIMARY KEY (parent, name))')
        self._conn.commit()

    def save_items(self, by_parent, ts):
        # 同一 parent 整体替换：先删后插，被删掉的旧子项不残留
        with self._lock:
            with self._conn:
                for parent, items in by_parent.items():
                    self._conn.execute('DELETE FROM item WHERE parent = ?', (parent,))
                    self._conn.executemany(
                        'INSERT INTO item(parent, name, size, type, scanned_at) VALUES (?, ?, ?, ?, ?)',
                        [(parent, n, s, t, ts) for n, s, t in items])

    def load_items(self, path):
        """返回 ([(name, size, type)按大小降序], 时间)；从未分析过返回 None"""
        with self._lock:
            rows = self._conn.execute(
                'SELECT name, size, type, scanned_at FROM item WHERE parent = ? '
                'ORDER BY size DESC', (canon(path),)).fetchall()
        if not rows:
            return None
        return [(n, s, t) for n, s, t, _ in rows], rows[0][3]


# ---- 界面状态持久化：app.py 同级 ui_state.json，逻辑像素+分隔条比例，不随缩放比例变化 ----
UI_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ui_state.json')


def load_ui_state():
    try:
        with open(UI_STATE_PATH, encoding='utf-8') as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def save_ui_state(st):
    try:
        with open(UI_STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump(st, f)
    except OSError:
        pass


def start_dir(state):
    """上次关窗时选中的目录；不存在（删了/盘符没插）则回退当前目录"""
    d = state.get('dir')
    return d if isinstance(d, str) and os.path.isdir(d) else os.getcwd()


# ==================== 专项清理：已知安全/可控占用项的规则注册表 ====================
# 每条规则 = 一个 find()，返回待清理路径列表；路径在扫描时才从环境变量解析
# （测试可把 LOCALAPPDATA/TEMP 指到夹具目录，不碰真实数据）

def _temp_root():
    return os.environ.get('TEMP') or os.environ.get('TMP') or ''


def _lad():
    return os.environ.get('LOCALAPPDATA') or ''


_VS_JUNK = re.compile(r'[a-z0-9]{8}\.[a-z0-9]{3}$')


def find_vsinstaller():
    """%TEMP% 下 xxxxxxxx.xxx 格式且含 vs_installer.exe 的文件夹：
    VS Installer 每次自我更新就解压一份完整副本，旧副本从不清理（实测两月攒 475 份 / 14 GB）"""
    t = _temp_root()
    if not t or not os.path.isdir(t):
        return []
    out = []
    for e in os.scandir(t):
        try:
            if (e.is_dir(follow_symlinks=False) and _VS_JUNK.fullmatch(e.name)
                    and os.path.isfile(os.path.join(e.path, 'vs_installer.exe'))):
                out.append(e.path)
        except OSError:
            pass
    return sorted(out)


def find_temp_misc():
    """%TEMP% 下除 VS Installer 残留外的其余所有项（被占用的删除时会自动跳过）"""
    t = _temp_root()
    if not t or not os.path.isdir(t):
        return []
    skip = {canon(p) for p in find_vsinstaller()}
    out = []
    for e in os.scandir(t):
        try:
            if canon(e.path) not in skip:
                out.append(e.path)
        except OSError:
            pass
    return sorted(out)


def _contents(*parts):
    """目录的内容项列表：保留目录本身，只清里面"""
    base = os.path.join(_lad(), *parts) if _lad() else ''
    if not base or not os.path.isdir(base):
        return []
    try:
        return sorted(e.path for e in os.scandir(base))
    except OSError:
        return []


def find_unity_cache():
    return _contents('Unity', 'cache')


def find_tuanjie_cache():
    return _contents('Tuanjie', 'cache')


def find_npm_cache():
    return _contents('npm-cache')


def find_crashdumps():
    return _contents('CrashDumps')


def find_pnpm_store():
    # 只清 store 子目录：%LOCALAPPDATA%\pnpm 本身可能装着 pnpm 本体，不能动
    return _contents('pnpm', 'store')


def find_edge_sw():
    base = os.path.join(_lad(), 'Microsoft', 'Edge', 'User Data', 'Default', 'Service Worker')
    return [p for p in (os.path.join(base, s) for s in ('CacheStorage', 'ScriptStorage'))
            if os.path.isdir(p)]


class Rule:
    """一条清理规则。proc 非空时：清理前检测到该进程在运行则整条跳过（文件被占用/防误伤）"""

    def __init__(self, rid, name, risk, desc, find, default, proc=None):
        self.id, self.name, self.risk, self.desc = rid, name, risk, desc
        self.find, self.default, self.proc = find, default, proc


RULES = [
    Rule('temp_vsinstaller', 'VS Installer 更新残留', '安全',
         'VS 安装器每次自我更新解压的副本（随机名文件夹），从不自动清理', find_vsinstaller, True),
    Rule('temp_misc', 'Temp 其他临时文件', '注意',
         '其余临时文件；被程序占用的会自动跳过，建议先关闭正在安装/更新的程序', find_temp_misc, False),
    Rule('unity_cache', 'Unity Hub 下载缓存', '安全',
         '编辑器安装包下载缓存，删除不影响已安装的编辑器', find_unity_cache, True),
    Rule('tuanjie_cache', '团结引擎 Hub 下载缓存', '安全',
         '同上（Unity 中国版），删除不影响已安装的编辑器', find_tuanjie_cache, True),
    Rule('npm_cache', 'npm 缓存', '安全', 'npm 下载缓存，之后安装包会重新下载', find_npm_cache, True),
    Rule('crashdumps', '程序崩溃转储', '安全', '软件崩溃时留下的 .dmp 调试文件', find_crashdumps, True),
    Rule('pnpm_store', 'pnpm 存储', '注意',
         'pnpm 全局包存储；删除后首次安装依赖会重新下载（不碰 pnpm 本体）', find_pnpm_store, False),
    Rule('edge_sw', 'Edge 网站离线缓存', '注意',
         'Service Worker 缓存的网站离线数据；需先完全关闭 Edge，部分 PWA 离线数据会丢',
         find_edge_sw, False, proc='msedge.exe'),
]


def proc_running(image):
    """按映像名检测进程是否在运行"""
    if sys.platform != 'win32':
        return False
    try:
        r = subprocess.run(['tasklist', '/FI', f'IMAGENAME eq {image}', '/NH'],
                           capture_output=True, text=True, timeout=15,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return image.lower() in r.stdout.lower()
    except Exception:
        return False


# ---- 删除：默认移入回收站（可恢复）；取消勾选则永久删除 ----
_FO_DELETE = 0x0003
_FOF_SILENT = 0x0004
_FOF_NOCONFIRMATION = 0x0010
_FOF_ALLOWUNDO = 0x0040
_FOF_NOERRORUI = 0x0400
_FOF_WANTNUKEWARNING = 0x4000  # 超出回收站容量时报错返回，而不是静默降级成永久删除


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [('hwnd', ctypes.c_void_p), ('wFunc', ctypes.c_uint),
                ('pFrom', ctypes.c_wchar_p), ('pTo', ctypes.c_wchar_p),
                ('fFlags', ctypes.c_ushort), ('fAnyOperationsAborted', ctypes.c_int),
                ('hNameMappings', ctypes.c_void_p), ('lpszProgressTitle', ctypes.c_wchar_p)]


def _recycle_call(paths):
    """一批路径一次 API 调用（pFrom 是多字符串：\\0 分隔、字符串结尾符再补一个 \\0）。返回 0 成功"""
    buf = '\0'.join(os.path.abspath(p) for p in paths) + '\0'
    op = _SHFILEOPSTRUCTW()
    op.wFunc = _FO_DELETE
    op.pFrom = buf
    op.fFlags = (_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT
                 | _FOF_NOERRORUI | _FOF_WANTNUKEWARNING)
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))


def recycle_paths(paths):
    """移入回收站：分块批量执行（每块一次 API）；块失败时逐项重试定位问题项。
    返回 (成功列表, 失败列表)。失败项原样保留，绝不悄悄降级成永久删除"""
    ok, failed = [], []
    paths = list(paths)
    for i in range(0, len(paths), 200):
        chunk = paths[i:i + 200]
        try:
            rc = _recycle_call(chunk)
        except Exception:
            rc = -1
        if rc == 0:
            ok.extend(chunk)
        else:
            for p in chunk:
                try:
                    rc = _recycle_call([p])
                except Exception:
                    rc = -1
                (ok if rc == 0 else failed).append(p)
    # API 报 0 但个别项实际还在（少见），按还在算失败
    gone = [p for p in ok if not os.path.exists(p)]
    return gone, failed + [p for p in ok if os.path.exists(p)]


def _perm_delete(path):
    """永久删除单个路径；只读文件（如 git packfile）先去只读属性再删"""
    if os.path.isdir(path) and not os.path.islink(path):
        def onexc(func, p, _exc):
            os.chmod(p, stat.S_IWRITE)
            func(p)
        shutil.rmtree(path, onexc=onexc)
    else:
        os.remove(path)


def disk_free_text():
    d = os.environ.get('SystemDrive', 'C:') + '\\'
    try:
        return f'{d[:2]} 盘剩余 {fmt_size(shutil.disk_usage(d).free)}'
    except OSError:
        return ''


class CleanupTab(ttk.Frame):
    """「专项清理」分页：规则化专项扫描 + 汇总统计面板 + 一键清理（过程可视）。
    线程模型与主界面一致：工作线程只碰 queue，主线程 poll 刷新（tk 非线程安全）"""

    def __init__(self, master, state, scale=1.0):
        super().__init__(master)
        self.q = queue.Queue()   # 本分页的事件队列（由 App._poll 驱动 poll()）
        self.results = {}        # rule_id -> [(path, size)]，扫描填充
        self.checked = {}        # rule_id -> bool
        self.busy = False        # 扫描或清理进行中
        self._last_clean = ''    # 上次清理结果摘要（显示在汇总第二行）
        self.confirm = lambda text: messagebox.askyesno('确认清理', text, parent=self)

        st = state.get('cleanup') if isinstance(state.get('cleanup'), dict) else {}
        saved_sel = st.get('sel') if isinstance(st.get('sel'), list) else None
        for r in RULES:  # 有记忆按记忆，没有按规则默认（安全项勾选）
            self.checked[r.id] = (r.id in saved_sel) if saved_sel is not None else r.default
        self.recycle_var = tk.BooleanVar(value=bool(st.get('recycle', True)))

        # 工具栏
        bar = ttk.Frame(self)
        bar.pack(fill='x', padx=8, pady=(8, 4))
        self.scan_btn = ttk.Button(bar, text='开始扫描', command=self.start_scan)
        self.scan_btn.pack(side='left')
        ttk.Button(bar, text='勾选安全项', command=self.check_safe).pack(side='left', padx=(6, 0))
        self.clean_btn = ttk.Button(bar, text='一键清理', command=self.start_clean,
                                    state='disabled')
        self.clean_btn.pack(side='left', padx=(6, 0))
        ttk.Checkbutton(bar, text='移入回收站（可恢复）',
                        variable=self.recycle_var).pack(side='left', padx=(12, 0))

        # 汇总统计面板
        panel = ttk.LabelFrame(self, text='汇总')
        panel.pack(fill='x', padx=8, pady=(0, 4))
        self.sum_var1 = tk.StringVar(value='尚未扫描 —— 点「开始扫描」查找可清理项')
        self.sum_var2 = tk.StringVar(value=disk_free_text())
        ttk.Label(panel, textvariable=self.sum_var1, anchor='w').pack(fill='x', padx=6, pady=(3, 0))
        ttk.Label(panel, textvariable=self.sum_var2, anchor='w').pack(fill='x', padx=6, pady=(0, 3))

        # 清理过程：进度条 + 当前动作（pack 顺序决定布局：先底栏后中间展开区）
        prog = ttk.Frame(self)
        prog.pack(side='bottom', fill='x', padx=8, pady=(4, 8))
        self.prog_var = tk.StringVar(value='')
        ttk.Label(prog, textvariable=self.prog_var, anchor='w').pack(fill='x')
        self.prog = ttk.Progressbar(prog, maximum=100)
        self.prog.pack(fill='x', pady=(2, 0))
        log_box = ttk.LabelFrame(self, text='清理过程')
        log_box.pack(side='bottom', fill='both', padx=8, pady=(0, 4))
        self.log_view = tk.Text(log_box, height=9, state='disabled', wrap='word',
                                font=('Consolas', 9))
        lsb = ttk.Scrollbar(log_box, orient='vertical', command=self.log_view.yview)
        self.log_view.config(yscrollcommand=lsb.set)
        self.log_view.pack(side='left', fill='both', expand=True)
        lsb.pack(side='right', fill='y')

        # 规则列表：首列模拟勾选框（点单元格切换 ☑/☐），双击行查看命中的具体路径
        cols = ('sel', 'name', 'size', 'count', 'risk', 'desc')
        tree_box = ttk.Frame(self)
        tree_box.pack(side='top', fill='both', expand=True, padx=8, pady=(0, 4))
        self.rules = ttk.Treeview(tree_box, columns=cols, show='headings', height=8)
        for col, text, w, anchor in (('sel', '选', 36, 'center'),
                                     ('name', '规则', 170, 'w'),
                                     ('size', '大小', 100, 'e'),
                                     ('count', '目标数', 60, 'e'),
                                     ('risk', '风险', 50, 'center'),
                                     ('desc', '说明', 380, 'w')):
            self.rules.heading(col, text=text)
            self.rules.column(col, width=int(w * scale), anchor=anchor,
                              stretch=(col == 'desc'))
        for r in RULES:
            self.rules.insert('', 'end', iid=r.id,
                              values=(self._sel_mark(r.id), r.name, '—', '—', r.risk,
                                      r.desc))
        rsb = ttk.Scrollbar(tree_box, orient='vertical', command=self.rules.yview)
        self.rules.config(yscrollcommand=rsb.set)
        self.rules.pack(side='left', fill='both', expand=True)
        rsb.pack(side='right', fill='y')
        self.rules.bind('<Button-1>', self._on_rule_click)
        self.rules.bind('<Double-1>', self._on_rule_dblclick)

    # ---- 勾选状态 ----
    def _sel_mark(self, rid):
        return '☑' if self.checked.get(rid) else '☐'

    def set_checked(self, ids):
        ids = set(ids)
        for r in RULES:
            self.checked[r.id] = r.id in ids
            self.rules.item(r.id, values=(self._sel_mark(r.id), r.name,
                                          *self.rules.item(r.id)['values'][2:]))
        self._update_summary()
        self._refresh_clean_btn()

    def check_safe(self):
        self.set_checked(r.id for r in RULES if r.risk == '安全')

    def _on_rule_click(self, e):
        if self.busy:
            return
        row = self.rules.identify_row(e.y)
        if row and self.rules.identify_column(e.x) == '#1':
            self.checked[row] = not self.checked.get(row, False)
            vals = self.rules.item(row)['values']
            self.rules.item(row, values=(self._sel_mark(row), *vals[1:]))
            self._update_summary()
            self._refresh_clean_btn()

    def _on_rule_dblclick(self, e):
        row = self.rules.identify_row(e.y)
        items = self.results.get(row or '')
        if not items:
            return
        rule = next(r for r in RULES if r.id == row)
        top = tk.Toplevel(self)
        top.title(f'{rule.name} —— 命中 {len(items)} 项')
        txt = tk.Text(top, wrap='none', font=('Consolas', 9))
        sb = ttk.Scrollbar(top, orient='vertical', command=txt.yview)
        txt.config(yscrollcommand=sb.set)
        txt.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        for p, s in items:
            txt.insert('end', f'{fmt_size(s):>10}  {safe(p)}\n')
        txt.config(state='disabled')

    # ---- 汇总面板 ----
    def _update_summary(self):
        if not self.results:
            return
        total = sum(s for items in self.results.values() for _, s in items)
        n = sum(len(items) for items in self.results.values())
        hit = sum(1 for items in self.results.values() if items)
        sel = sum(s for r in RULES if self.checked.get(r.id)
                  for _, s in self.results.get(r.id, []))
        self.sum_var1.set(f'可清理 {fmt_size(total)}（{hit}/{len(RULES)} 项规则命中，'
                          f'共 {n} 个目标）｜已勾选 {fmt_size(sel)}')

    def _refresh_var2(self):
        parts = [disk_free_text(), self._last_clean]
        self.sum_var2.set('｜'.join(p for p in parts if p))

    def _refresh_clean_btn(self):
        ready = bool(self.results) and any(
            self.checked.get(r.id) and self.results.get(r.id) for r in RULES)
        self.clean_btn.config(state='normal' if ready and not self.busy else 'disabled')

    # ---- 扫描 ----
    def start_scan(self):
        if self.busy:
            return
        self.busy = True
        self.results = {}
        self.scan_btn.config(state='disabled')
        self.clean_btn.config(state='disabled')
        self.sum_var1.set('扫描中…')
        for r in RULES:
            vals = self.rules.item(r.id)['values']
            self.rules.item(r.id, values=(vals[0], r.name, '…', '…', r.risk, r.desc))
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        for r in RULES:
            t0 = time.time()
            try:
                paths = r.find()
            except Exception as e:
                log(f'专项扫描规则「{r.name}」查找失败: {e!r}')
                paths = []
            items = []
            for p in paths:
                try:
                    size = (dir_size(p) if os.path.isdir(p) and not os.path.islink(p)
                            else os.path.getsize(p))
                except OSError:
                    size = 0
                items.append((p, size))
            items.sort(key=lambda x: x[1], reverse=True)
            hint = ''
            if items and r.proc and proc_running(r.proc):
                hint = f'（检测到 {r.proc} 运行中，清理时将跳过）'
            log(f'专项扫描: {r.name} 命中 {len(items)} 项 '
                f'{fmt_size(sum(s for _, s in items))}，耗时 {time.time() - t0:.1f}s')
            self.q.put(('scan_rule', r.id, items, hint))
        self.q.put(('scan_done',))

    # ---- 清理 ----
    def start_clean(self):
        if self.busy:
            return
        selected = [r for r in RULES if self.checked.get(r.id) and self.results.get(r.id)]
        if not selected:
            return
        total = sum(s for r in selected for _, s in self.results[r.id])
        n = sum(len(self.results[r.id]) for r in selected)
        mode = '移入回收站' if self.recycle_var.get() else '永久删除'
        lines = '\n'.join(f'· {r.name}（{fmt_size(sum(s for _, s in self.results[r.id]))}）'
                          for r in selected)
        if not self.confirm(f'将以【{mode}】方式清理以下 {len(selected)} 项规则'
                            f'（共 {n} 个目标，{fmt_size(total)}）：\n\n{lines}\n\n继续？'):
            return
        self.busy = True
        self.scan_btn.config(state='disabled')
        self.clean_btn.config(state='disabled')
        self.prog.config(maximum=max(n, 1), value=0)
        threading.Thread(target=self._clean_worker,
                         args=(selected, self.recycle_var.get()), daemon=True).start()

    def _clean_worker(self, selected, recycle):
        freed, ok_n, skip_n, fail_n = 0, 0, 0, 0
        total = sum(len(self.results[r.id]) for r in selected)
        done = 0
        for r in selected:
            items = self.results.get(r.id, [])
            self.q.put(('clean_log', f'—— {r.name}（{len(items)} 项，{mode_str(recycle)}）——'))
            if r.proc and proc_running(r.proc):
                self.q.put(('clean_log',
                            f'⚠ 检测到 {r.proc} 正在运行，整条跳过（请先关闭再清理）'))
                skip_n += len(items)
                done += len(items)
                self.q.put(('clean_step', done, total, r.name))
                continue
            if recycle:
                sizes = {canon(p): s for p, s in items}
                stale = [p for p, _ in items
                         if not os.path.exists(p) and not os.path.islink(p)]
                stale_set = set(stale)
                if stale:  # 扫描后被别的东西删掉了，算跳过而非失败
                    skip_n += len(stale)
                    for p in stale:
                        self.q.put(('clean_log', f'⊘ 已不存在: {safe(p)}'))
                ok, failed = recycle_paths([p for p, _ in items if p not in stale_set])
                ok_n += len(ok)
                freed += sum(sizes.get(canon(p), 0) for p in ok)
                fail_n += len(failed)
                if ok:
                    self.q.put(('clean_log',
                                f'✓ 已移入回收站 {len(ok)} 项'
                                f'（{fmt_size(sum(sizes.get(canon(p), 0) for p in ok))}）'))
                for p in failed:
                    self.q.put(('clean_log', f'✗ 回收站收不下或删除失败: {safe(p)}'))
                done += len(items)
                self.q.put(('clean_step', done, total, r.name))
            else:
                for p, size in items:
                    if not os.path.exists(p) and not os.path.islink(p):
                        skip_n += 1
                        self.q.put(('clean_log', f'⊘ 已不存在: {safe(p)}'))
                    else:
                        try:
                            _perm_delete(p)
                            ok_n += 1
                            freed += size
                            self.q.put(('clean_log', f'✓ 已删除: {safe(p)}'))
                        except OSError as e:
                            fail_n += 1
                            self.q.put(('clean_log',
                                        f'✗ 删除失败: {safe(p)}（{e.strerror or e}）'))
                    done += 1
                    self.q.put(('clean_step', done, total, r.name))
        log(f'专项清理完成: 释放 {fmt_size(freed)}，成功 {ok_n}，跳过 {skip_n}，失败 {fail_n}')
        self.q.put(('clean_done', freed, ok_n, skip_n, fail_n))

    # ---- 事件处理（主线程 poll） ----
    def poll(self):
        try:
            while True:
                self._handle(*self.q.get_nowait())
        except queue.Empty:
            pass

    def _handle(self, kind, *args):
        if kind == 'scan_rule':
            rid, items, hint = args
            self.results[rid] = items
            rule = next(r for r in RULES if r.id == rid)
            self.rules.item(rid, values=(self._sel_mark(rid), rule.name,
                                         fmt_size(sum(s for _, s in items)),
                                         len(items), rule.risk, rule.desc + hint))
            self._update_summary()
        elif kind == 'scan_done':
            self.busy = False
            self.scan_btn.config(state='normal')
            self._update_summary()
            self._refresh_var2()
            self._refresh_clean_btn()
            n = sum(len(v) for v in self.results.values())
            self.prog_var.set(f'扫描完成：{n} 个目标' if n else '扫描完成：没有发现可清理项')
        elif kind == 'clean_log':
            self._append(args[0])
        elif kind == 'clean_step':
            done, total, name = args
            self.prog.config(value=done)
            self.prog_var.set(f'清理中：{name}（{done}/{total}）')
        elif kind == 'clean_done':
            freed, ok_n, skip_n, fail_n = args
            self.busy = False
            self.scan_btn.config(state='normal')
            self._last_clean = (f'本次释放 {fmt_size(freed)}'
                                f'（成功 {ok_n}，跳过 {skip_n}，失败 {fail_n}）')
            self.prog_var.set('清理完成，正在重新扫描…')
            self._append(f'════ 清理完成：释放 {fmt_size(freed)}，'
                         f'成功 {ok_n} 项，跳过 {skip_n} 项，失败 {fail_n} 项 ════')
            self.start_scan()  # 重新扫描：列表和汇总数字刷新为真实剩余

    def _append(self, line):
        self.log_view.config(state='normal')
        self.log_view.insert('end', safe(line) + '\n')
        n = int(self.log_view.index('end-1c').split('.')[0])
        if n > 2000:
            self.log_view.delete('1.0', f'{n - 2000}.0')
        self.log_view.see('end')
        self.log_view.config(state='disabled')

    def ui_state(self):
        return {'sel': [r.id for r in RULES if self.checked.get(r.id)],
                'recycle': bool(self.recycle_var.get())}


def mode_str(recycle):
    return '移入回收站' if recycle else '永久删除'


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('文件夹大小查看器')
        self.state = load_ui_state()  # 窗口位置/大小（逻辑像素）+ 左右分隔条比例 + 上次定位的目录
        self._apply_geometry()
        style = ttk.Style(self)
        # 行高默认=字体行高（150% 缩放下都是 20px），文字下沿被行边界裁掉，留点空隙
        style.configure('Treeview', rowheight=tkfont.Font(self).metrics('linespace') + 5)
        # Windows 下 LabelFrame 标题压在边框线上，文字下沿被遮，底部让出一点
        style.configure('TLabelframe.Label', padding=(2, 0, 2, 3))
        self.loaded = set()   # 已展开加载过的树节点（完整路径）
        self.current = ''     # 左侧当前选中路径
        self.store = Store()  # 必须在 navigate 前建好（set_current 会查库）

        s = self._eff_scale()  # DPI 缩放比：分隔条宽、列宽都按它放大，不同缩放下视觉一致

        # 分页：磁盘分析（原有目录树浏览）+ 专项清理（已知安全项扫描/一键清理）
        self.nb = ttk.Notebook(self)
        tab1 = ttk.Frame(self.nb)
        self.nb.add(tab1, text='磁盘分析')
        self.cleanup_tab = CleanupTab(self.nb, self.state, s)
        self.nb.add(self.cleanup_tab, text='专项清理')
        self.nb.pack(fill='both', expand=True)

        # 顶栏：路径输入框 + 转到按钮
        top = ttk.Frame(tab1)
        top.pack(fill='x', padx=8, pady=6)
        self.path_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.path_var)
        entry.pack(side='left', fill='x', expand=True)
        entry.bind('<Return>', lambda e: self.navigate(self.path_var.get()))
        ttk.Button(top, text='转到',
                   command=lambda: self.navigate(self.path_var.get())).pack(side='left', padx=(6, 0))

        # 左右布局不用 ttk.PanedWindow：原生 sash 只有 ~4px 太细，且 Windows vista 主题
        # 忽略 sashwidth 无法加粗。改用自绘分隔条：10 逻辑像素宽条 + 中间 1px 细线提示可拖动，
        # 悬停/按下时加深，光标变为双向箭头。
        self.paned = tk.Frame(tab1)
        self.paned.pack(fill='both', expand=True, padx=8, pady=(0, 8))

        # 左侧：目录树（懒加载，双击展开即打开）+ 底部分析按钮
        # pack_propagate(0)：带子件的 Frame 会忽略 width 选项（请求尺寸由子件决定），
        # 关掉传播后 width 才生效，拖动/缩放改 width 才能真正改变布局
        left = ttk.Frame(self.paned, width=240)
        left.pack_propagate(0)
        self.left_frame = left
        left.pack(side='left', fill='both')
        tree_box = ttk.Frame(left)
        tree_box.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(tree_box, show='tree')
        tsb = ttk.Scrollbar(tree_box, orient='vertical', command=self.tree.yview)
        self.tree.config(yscrollcommand=tsb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        tsb.pack(side='right', fill='y')
        btns = ttk.Frame(left)
        btns.pack(fill='x', pady=(6, 0))
        self.btn_var = tk.StringVar(value='分析')
        self.btn = ttk.Button(btns, textvariable=self.btn_var, command=self.analyze)
        self.btn.pack(side='left', fill='x', expand=True)
        self.btn2_var = tk.StringVar(value='递归分析')
        self.btn2 = ttk.Button(btns, textvariable=self.btn2_var, command=self.analyze_recursive)
        self.btn2.pack(side='left', fill='x', expand=True, padx=(6, 0))
        self.tree.bind('<<Treeview-Open>>', lambda e: self._ensure(self.tree.focus()))
        # 鼠标点「+」箭头时 focus 还是旧节点，靠 Open 事件会加载错节点；按下时就预加载
        self.tree.bind('<ButtonPress-1>', self._on_press)
        for c in string.ascii_uppercase:  # 盘符作为根节点
            root = f'{c}:\\'
            if os.path.isdir(root):
                self.tree.insert('', 'end', iid=root, text=root)
                if has_subdirs(root):
                    self.tree.insert(root, 'end', text='…')  # 占位子节点，让「+」出现

        # 分隔条：拖它调左右比例（条中间那根细线就是可拖动的提示）。
        # 用 Canvas 而非 Frame：无子件的 Frame 不认 width 选项、fill='y' 也不生效，
        # Canvas 是叶控件，尺寸选项可靠，细线随 <Configure> 重画。
        self.sash = tk.Canvas(self.paned, width=int(10 * s), bg='#e7e7e7',
                              highlightthickness=0, cursor='size_we')
        self._sash_line = self.sash.create_line(0, 0, 0, 0, fill='#b8b8b8')
        self.sash.pack(side='left', fill='y')
        self.sash.bind('<Configure>', self._sash_layout)
        self._sash_pressed = False
        self._sash_ratio = 1 / 3  # 默认左右 1:2（与原 PanedWindow weight 一致），关窗存值、启动时恢复
        self.sash.bind('<ButtonPress-1>', self._sash_press)
        self.sash.bind('<B1-Motion>', self._sash_motion)
        self.sash.bind('<ButtonRelease-1>', self._sash_release)
        self.sash.bind('<Enter>', lambda e: self._sash_colors('hover'))
        self.sash.bind('<Leave>', lambda e: self._sash_colors('normal'))
        self.paned.bind('<Configure>', self._on_paned_configure)

        # 右侧：子项列表 + 底部日志模块 + 数据时间状态栏
        right = ttk.Frame(self.paned)
        right.pack(side='left', fill='both', expand=True)
        self.status_var = tk.StringVar(value='')
        ttk.Label(right, textvariable=self.status_var, anchor='w').pack(
            side='bottom', fill='x', padx=2, pady=(2, 0))
        # 日志模块：显示 app.py 打印到终端的日志（工作线程入队，_poll 取出来刷新）
        log_box = ttk.LabelFrame(right, text='日志')
        log_box.pack(side='bottom', fill='x', padx=2, pady=(4, 2))
        self.log_view = tk.Text(log_box, height=8, state='disabled', wrap='word',
                                font=('Consolas', 9))
        lsb2 = ttk.Scrollbar(log_box, orient='vertical', command=self.log_view.yview)
        self.log_view.config(yscrollcommand=lsb2.set)
        self.log_view.pack(side='left', fill='both', expand=True)
        lsb2.pack(side='right', fill='y')
        self.list = ttk.Treeview(right, columns=('name', 'size', 'type'), show='headings')
        for col, text, w, anchor in (('name', '名称', 380, 'w'),
                                     ('size', '大小', 110, 'e'),
                                     ('type', '类型', 70, 'center')):
            self.list.heading(col, text=text)
            self.list.column(col, width=int(w * s), anchor=anchor)
        lsb = ttk.Scrollbar(right, orient='vertical', command=self.list.yview)
        self.list.config(yscrollcommand=lsb.set)
        self.list.pack(side='left', fill='both', expand=True)
        lsb.pack(side='right', fill='y')

        # 右键菜单（右侧列表）：复制名称 / 复制路径 / 打开文件夹；点文件夹行等同左侧点击该目录
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label='复制名称', command=lambda: self._copy('name'))
        self.menu.add_command(label='复制路径', command=lambda: self._copy('path'))
        self.menu.add_command(label='打开文件夹', command=self._open_from_list)  # 文件行上禁用
        # Windows 下 Tk 的 menu add 返回空串（add_command → None），entry 索引要靠 label 查，
        # 否则 entryconfig(None) 报 "bad menu entry index -state"
        self.open_item = self.menu.index('打开文件夹')
        self.list.bind('<Button-3>', self._on_right_click)
        self.list.bind('<Button-1>', self._on_list_click)
        # 右键菜单（左侧目录树）：同样三项，对象是右键所在的树节点
        self.menu_tree = tk.Menu(self, tearoff=0)
        self.menu_tree.add_command(label='复制名称', command=lambda: self._copy_tree('name'))
        self.menu_tree.add_command(label='复制路径', command=lambda: self._copy_tree('path'))
        self.menu_tree.add_command(label='打开文件夹', command=self._open_from_tree)
        self.tree.bind('<Button-3>', self._on_tree_right_click)

        self.protocol('WM_DELETE_WINDOW', self._close)  # 关窗时存界面状态
        self.after(150, self._restore_sash)  # 窗口拿到实际尺寸后恢复分隔条位置
        tab_i = self.state.get('tab')  # 恢复上次所在分页
        if isinstance(tab_i, int) and 0 <= tab_i < self.nb.index('end'):
            self.nb.select(tab_i)

        self.navigate(start_dir(self.state))  # 恢复上次定位的目录（树逐级展开+选中+滚动可见）
        self.q = queue.Queue()  # 扫描线程 → 主线程
        self._poll()

    # ---- 窗口状态：关窗存（位置/大小/分隔条），启动恢复；逻辑像素存储，换缩放比例也不跑偏 ----
    def _eff_scale(self):
        # 未声明感知时 winfo 坐标在虚拟 96 DPI 空间（=逻辑像素），比例按 1 算
        return dpi_scale(self.winfo_id()) if _DPI_AWARE else 1.0

    def _apply_geometry(self):
        s = self._eff_scale()
        g = self.state.get('geometry')
        if isinstance(g, dict):
            x, y, w, h = (g.get('x'), g.get('y'), g.get('w'), g.get('h'))
            if all(isinstance(v, (int, float)) for v in (x, y, w, h)) and 200 < w < 10000 and 200 < h < 10000:
                self.geometry(f'{int(w * s)}x{int(h * s)}{int(x * s):+d}{int(y * s):+d}')
                return
        self.geometry(f'{int(1000 * s)}x{int(560 * s)}')

    def _restore_sash(self):
        r = self.state.get('sash')
        if isinstance(r, (int, float)) and 0.05 <= r <= 0.95:
            self._sash_ratio = r
        self._apply_sash_ratio(self.paned.winfo_width())

    def _save_ui_state(self):
        if self.wm_state() in ('zoomed', 'fullscreen'):  # 最大化时位置/大小/分隔比例都失真，不存
            return
        s = self._eff_scale()
        st = {'geometry': {'x': round(self.winfo_x() / s), 'y': round(self.winfo_y() / s),
                           'w': round(self.winfo_width() / s), 'h': round(self.winfo_height() / s)}}
        if self.current and os.path.isdir(self.current):
            st['dir'] = self.current  # 上次定位的目录，下次启动左侧树直接展开选中
        st['tab'] = self.nb.index(self.nb.select())  # 上次所在分页
        st['cleanup'] = self.cleanup_tab.ui_state()  # 清理页勾选状态 + 回收站开关
        try:
            st['sash'] = round(self.left_frame.winfo_width() /
                               max(self.paned.winfo_width() - self.sash.winfo_width(), 1), 3)
        except tk.TclError:
            pass
        save_ui_state(st)

    def _close(self):
        self._save_ui_state()
        self.destroy()

    # ---- 分隔条：拖动改变左右比例；窗口缩放时按比例保持 ----
    _SASH_COLORS = {'normal': ('#e7e7e7', '#b8b8b8'),
                    'hover': ('#dcdcdc', '#8a8a8a'),
                    'pressed': ('#d0d0d0', '#5f5f5f')}

    def _sash_colors(self, state):
        if state == 'normal' and self._sash_pressed:
            return  # 拖动中指针扫出条外时不退出按下色
        bg, line = self._SASH_COLORS[state]
        self.sash.config(bg=bg)
        self.sash.itemconfig(self._sash_line, fill=line)

    def _sash_layout(self, e):
        # 细线竖直居中，窗口高度变化（含首次布局）时重画
        self.sash.coords(self._sash_line, e.width / 2, 0, e.width / 2, e.height)

    def _sash_press(self, e):
        self._sash_pressed = True
        self._sash_press_dx = e.x  # 按下点在条内的横向偏移，拖动时保持手与条的相对位置
        self._sash_colors('pressed')
        try:
            self.sash.grab_set()
        except tk.TclError:
            pass

    def _sash_release(self, e):
        self._sash_pressed = False
        try:
            self.sash.grab_release()
        except tk.TclError:
            pass
        inside = 0 <= e.x < self.sash.winfo_width() and 0 <= e.y < self.sash.winfo_height()
        self._sash_colors('hover' if inside else 'normal')

    def _sash_motion(self, e):
        if not self._sash_pressed:
            return
        left_w = e.x_root - self.paned.winfo_rootx() - self._sash_press_dx
        self._set_split(left_w)

    def _set_split(self, left_w):
        W = self.paned.winfo_width()
        sash_w = self.sash.winfo_width()
        min_w = int(80 * self._eff_scale())  # 左右两侧至少各留 80 逻辑像素
        hi = max(min_w, W - sash_w - min_w)
        left_w = max(min_w, min(hi, left_w))
        self.left_frame.config(width=left_w)
        self._sash_ratio = left_w / max(W - sash_w, 1)

    def _on_paned_configure(self, e):
        # 窗口尺寸变化时按当前比例重排；拖动分隔条不改变容器尺寸，不会触发
        self._apply_sash_ratio(e.width)

    def _apply_sash_ratio(self, W):
        sash_w = self.sash.winfo_width()
        if W <= sash_w + 2:
            return
        self.left_frame.config(width=int(round(self._sash_ratio * (W - sash_w))))

    # ---- 左侧目录树 ----
    def _ensure(self, node):
        if node and node not in self.loaded:
            self._populate(node)

    def _populate(self, node):
        self.tree.delete(*self.tree.get_children(node))
        for e in subdirs(node):
            self.tree.insert(node, 'end', iid=e.path, text=e.name)
            if has_subdirs(e.path):
                self.tree.insert(e.path, 'end', text='…')
        self.loaded.add(node)

    def navigate(self, path):
        path = os.path.normpath(path.strip())
        if not os.path.isdir(path):
            self.list.delete(*self.list.get_children())
            self.list.insert('', 'end', values=(f'路径不存在: {safe(path)}', '', ''))
            return
        drive, rest = os.path.splitdrive(path)
        node = drive.upper() + os.sep
        if self.tree.exists(node):
            self._ensure(node)
            for part in rest.split(os.sep):  # 逐级展开到目标
                if not part:
                    continue
                nxt = os.path.join(node, part)
                if not self.tree.exists(nxt):
                    break
                node = nxt
                self._ensure(node)
                self.tree.item(node, open=True)
            self.tree.selection_set(node)
            self.tree.focus(node)
            self.tree.see(node)
            self.set_current(node)  # Select 事件要等事件循环，直接同步设置

    def _on_press(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self._ensure(row)

    def _sync_selection(self):
        # <<Treeview-Select>> 对程序/键盘选择不触发，轮询最可靠
        sel = self.tree.selection()
        if sel and sel[0] != self.current:
            self.set_current(sel[0])

    def set_current(self, path):
        self.current = path
        self.path_var.set(safe(path))  # 顶栏跟随左侧选中
        res = self.store.load_items(path)
        if res:
            self._show_items(path, res[0], res[1], fresh=False)
        else:
            self.list.delete(*self.list.get_children())
            self.list.insert('', 'end', values=('（无数据，点『分析』或『递归分析』扫描）', '', ''))
            self.status_var.set('')

    # ---- 右侧分析 ----
    def analyze(self):
        self._start_scan(recursive=False)

    def analyze_recursive(self):
        self._start_scan(recursive=True)

    def _start_scan(self, recursive):
        path = self.current
        if not path or not os.path.isdir(path):
            return
        label = '递归分析' if recursive else '分析'
        log(f'点击{label}: {safe(path)}')
        self.btn.config(state='disabled')
        self.btn2.config(state='disabled')
        (self.btn2_var if recursive else self.btn_var).set(label + '中…')
        self.list.delete(*self.list.get_children())
        self.status_var.set('')
        threading.Thread(target=self._scan, args=(path, recursive), daemon=True).start()

    def _scan(self, path, recursive):
        # 子线程只碰 queue/store，不碰 tk（tk 非线程安全）
        try:
            if recursive:
                by_parent, _total = recursive_scan(path)
                err = None
            else:
                items, err = scan(path), None
                by_parent = {canon(path): items}
        except Exception as e:
            by_parent, err = {}, e
            log(f'扫描线程异常: {e!r}')
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        if not err:
            self.store.save_items(by_parent, ts)
        self.q.put((path, by_parent, err, ts))
        log('结果已入队，等待主线程刷新')

    def _poll(self):
        self._sync_selection()
        self.cleanup_tab.poll()  # 专项清理页的事件队列（扫描/清理进度）
        try:
            while True:
                self._fill(*self.q.get_nowait())
        except queue.Empty:
            pass
        except Exception as e:  # 显示出错也要继续轮询，不能静默死掉
            self._reset_buttons()
            self.list.delete(*self.list.get_children())
            self.list.insert('', 'end', values=(f'显示出错: {safe(repr(e))}', '', ''))
            self.status_var.set('')
        try:
            while True:
                self._append_log(LOG_Q.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _append_log(self, line):
        self.log_view.config(state='normal')
        self.log_view.insert('end', safe(line) + '\n')
        n = int(self.log_view.index('end-1c').split('.')[0])
        if n > 1000:  # 最多留 1000 行，防止长时间运行无限增长
            self.log_view.delete('1.0', f'{n - 1000}.0')
        self.log_view.see('end')
        self.log_view.config(state='disabled')

    def _reset_buttons(self):
        self.btn.config(state='normal')
        self.btn2.config(state='normal')
        self.btn_var.set('分析')
        self.btn2_var.set('递归分析')

    def _show_items(self, path, items, ts, fresh):
        self.list.delete(*self.list.get_children())
        if not items:
            self.list.insert('', 'end', values=('（空文件夹或无权限）', '', ''))
            self.status_var.set('')
            return
        for name, size, kind in items:
            self.list.insert('', 'end', iid=os.path.join(path, name),
                             values=(safe(name), fmt_size(size), kind))
        self.list.insert('', 'end', iid='total',
                         values=('— 合计 —', fmt_size(sum(s for _, s, _ in items)), ''))
        self.status_var.set(f'数据时间 {ts}（本次扫描）' if fresh else f'数据时间 {ts}（缓存）')

    def _fill(self, path, by_parent, err, ts):
        # 不丢弃结果：即使用户已切换目录也照常展示
        log(f'主线程收到结果: {len(by_parent)} 个目录 / '
            f'{sum(len(v) for v in by_parent.values())} 项，err={err!r}')
        self._reset_buttons()
        if err:
            self.list.delete(*self.list.get_children())
            self.list.insert('', 'end', values=(f'{safe(path)} 分析失败: {safe(repr(err))}', '', ''))
            self.status_var.set('')
            return
        self._show_items(path, by_parent.get(canon(path), []), ts, fresh=True)

    def _row_path(self, iid):
        # 行的 iid 就是子项完整路径；合计/错误行不是真实路径
        if iid and self.list.exists(iid) and os.path.isabs(iid):
            return iid
        return None

    def _on_list_click(self, event):
        # 点文件夹行 = 左侧点该目录：展开选中、顶栏输入框跟随、列表换成该目录的缓存
        p = self._row_path(self.list.identify_row(event.y))
        if p and os.path.isdir(p):
            self.navigate(p)

    def _on_right_click(self, event):
        row = self.list.identify_row(event.y)
        if self._row_path(row):
            self.list.selection_set(row)
            self.menu.entryconfig(self.open_item,
                                  state='normal' if os.path.isdir(row) else 'disabled')
            self.menu.post(event.x_root, event.y_root)

    def _copy(self, what):
        sel = self.list.selection()
        p = self._row_path(sel[0]) if sel else None
        if not p:
            return
        self.clipboard_clear()
        self.clipboard_append(os.path.basename(p) if what == 'name' else safe(p))

    # ---- 左侧树右键菜单 ----
    def _on_tree_right_click(self, event):
        node = self.tree.identify_row(event.y)
        if node:
            self.tree.selection_set(node)
            self.menu_tree.post(event.x_root, event.y_root)

    def _copy_tree(self, what):
        sel = self.tree.selection()
        if not sel:
            return
        p = sel[0]
        self.clipboard_clear()
        self.clipboard_append(self._node_name(p) if what == 'name' else safe(p))

    def _node_name(self, p):
        # 盘符根节点（D:\）没有 basename，用盘符当名称
        return os.path.basename(p) or p[:-1]

    # ---- 打开文件夹：系统资源管理器打开（应用内导航走左键，右键这项是外部打开） ----
    def _open_from_list(self):
        sel = self.list.selection()
        p = self._row_path(sel[0]) if sel else None
        if p and os.path.isdir(p):
            self._open_folder(p)

    def _open_from_tree(self):
        sel = self.tree.selection()
        if sel:
            self._open_folder(sel[0])

    def _open_folder(self, p):
        try:
            os.startfile(p)
        except OSError as e:
            log(f'无法打开文件夹: {safe(p)} {e!r}')


if __name__ == '__main__':
    enable_dpi_awareness()  # 必须先于任何 Tk 窗口创建（百分比缩放下文字发糊的修复）
    assert fmt_size(500) == '500 B' and fmt_size(2048) == '2.0 KB' and fmt_size(5 * 1024**3) == '5.0 GB'
    assert has_subdirs('.') in (True, False) and dir_size('.') > 0
    App().mainloop()
