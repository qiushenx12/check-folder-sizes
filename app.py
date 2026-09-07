# app.py — 左侧目录树（类似资源管理器），右侧按需分析当前目录的子项大小
# 分析结果缓存到同级 folder_sizes.db，切走再切回可回看；「递归分析」会把整棵子树逐层入库
import ctypes
import json
import os
import queue
import sqlite3
import string
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from concurrent.futures import ThreadPoolExecutor
from tkinter import ttk


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

        # 顶栏：路径输入框 + 转到按钮
        top = ttk.Frame(self)
        top.pack(fill='x', padx=8, pady=6)
        self.path_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.path_var)
        entry.pack(side='left', fill='x', expand=True)
        entry.bind('<Return>', lambda e: self.navigate(self.path_var.get()))
        ttk.Button(top, text='转到',
                   command=lambda: self.navigate(self.path_var.get())).pack(side='left', padx=(6, 0))

        s = self._eff_scale()  # DPI 缩放比：分隔条宽、列宽都按它放大，不同缩放下视觉一致
        # 左右布局不用 ttk.PanedWindow：原生 sash 只有 ~4px 太细，且 Windows vista 主题
        # 忽略 sashwidth 无法加粗。改用自绘分隔条：10 逻辑像素宽条 + 中间 1px 细线提示可拖动，
        # 悬停/按下时加深，光标变为双向箭头。
        self.paned = tk.Frame(self)
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
