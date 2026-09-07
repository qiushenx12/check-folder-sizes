# app.py — 左侧目录树（类似资源管理器），右侧按需分析当前目录的子项大小
import os
import queue
import string
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import ttk


def log(msg):
    print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)


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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('文件夹大小查看器')
        self.geometry('1000x560')
        self.loaded = set()   # 已展开加载过的树节点（完整路径）
        self.current = ''     # 左侧当前选中路径

        # 顶栏：路径输入框 + 转到按钮
        top = ttk.Frame(self)
        top.pack(fill='x', padx=8, pady=6)
        self.path_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.path_var)
        entry.pack(side='left', fill='x', expand=True)
        entry.bind('<Return>', lambda e: self.navigate(self.path_var.get()))
        ttk.Button(top, text='转到',
                   command=lambda: self.navigate(self.path_var.get())).pack(side='left', padx=(6, 0))

        paned = ttk.PanedWindow(self, orient='horizontal')
        paned.pack(fill='both', expand=True, padx=8, pady=(0, 8))

        # 左侧：目录树（懒加载，双击展开即打开）+ 底部分析按钮
        left = ttk.Frame(paned)
        paned.add(left, weight=1)
        tree_box = ttk.Frame(left)
        tree_box.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(tree_box, show='tree')
        tsb = ttk.Scrollbar(tree_box, orient='vertical', command=self.tree.yview)
        self.tree.config(yscrollcommand=tsb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        tsb.pack(side='right', fill='y')
        self.btn_var = tk.StringVar(value='分析')
        self.btn = ttk.Button(left, textvariable=self.btn_var, command=self.analyze)
        self.btn.pack(fill='x', pady=(6, 0))
        self.tree.bind('<<Treeview-Open>>', lambda e: self._ensure(self.tree.focus()))
        # 鼠标点「+」箭头时 focus 还是旧节点，靠 Open 事件会加载错节点；按下时就预加载
        self.tree.bind('<ButtonPress-1>', self._on_press)
        for c in string.ascii_uppercase:  # 盘符作为根节点
            root = f'{c}:\\'
            if os.path.isdir(root):
                self.tree.insert('', 'end', iid=root, text=root)
                if has_subdirs(root):
                    self.tree.insert(root, 'end', text='…')  # 占位子节点，让「+」出现

        # 右侧：子项列表 + 底部状态/分析按钮
        right = ttk.Frame(paned)
        paned.add(right, weight=2)
        self.list = ttk.Treeview(right, columns=('name', 'size', 'type'), show='headings')
        for col, text, w, anchor in (('name', '名称', 380, 'w'),
                                     ('size', '大小', 110, 'e'),
                                     ('type', '类型', 70, 'center')):
            self.list.heading(col, text=text)
            self.list.column(col, width=w, anchor=anchor)
        lsb = ttk.Scrollbar(right, orient='vertical', command=self.list.yview)
        self.list.config(yscrollcommand=lsb.set)
        self.list.pack(side='left', fill='both', expand=True)
        lsb.pack(side='right', fill='y')

        self.navigate(os.getcwd())
        self.q = queue.Queue()  # 扫描线程 → 主线程
        self._poll()

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
        self.list.delete(*self.list.get_children())

    # ---- 右侧分析 ----
    def analyze(self):
        path = self.current
        if not path or not os.path.isdir(path):
            return
        log(f'点击分析: {safe(path)}')
        self.btn.config(state='disabled')
        self.btn_var.set('分析中…')
        self.list.delete(*self.list.get_children())
        threading.Thread(target=self._scan, args=(path,), daemon=True).start()

    def _scan(self, path):
        # 子线程只碰 queue，不碰 tk（tk 非线程安全）
        try:
            items, err = scan(path), None
        except Exception as e:
            items, err = [], e
            log(f'扫描线程异常: {e!r}')
        self.q.put((path, items, err))
        log('结果已入队，等待主线程刷新')

    def _poll(self):
        self._sync_selection()
        try:
            while True:
                self._fill(*self.q.get_nowait())
        except queue.Empty:
            pass
        except Exception as e:  # 显示出错也要继续轮询，不能静默死掉
            self.btn.config(state='normal')
            self.btn_var.set('分析')
            self.list.delete(*self.list.get_children())
            self.list.insert('', 'end', values=(f'显示出错: {safe(repr(e))}', '', ''))
        self.after(100, self._poll)

    def _fill(self, path, items, err):
        # 不丢弃结果：即使用户已切换目录也照常展示
        log(f'主线程收到结果: {len(items)} 项，err={err!r}')
        self.btn.config(state='normal')
        self.btn_var.set('分析')
        self.list.delete(*self.list.get_children())
        if err:
            self.list.insert('', 'end', values=(f'{safe(path)} 分析失败: {safe(repr(err))}', '', ''))
        elif not items:
            self.list.insert('', 'end', values=('（空文件夹或无权限）', '', ''))
        else:
            for name, size, kind in items:
                self.list.insert('', 'end', values=(safe(name), fmt_size(size), kind))
            self.list.insert('', 'end', values=('— 合计 —', fmt_size(sum(s for _, s, _ in items)), ''))


if __name__ == '__main__':
    assert fmt_size(500) == '500 B' and fmt_size(2048) == '2.0 KB' and fmt_size(5 * 1024**3) == '5.0 GB'
    assert has_subdirs('.') in (True, False) and dir_size('.') > 0
    App().mainloop()
