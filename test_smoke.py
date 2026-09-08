# 端到端冒烟测试：展开 C:\、顶栏导航与同步、分析按钮、分析期间切换目录不丢结果
# 右键菜单（左右两侧均支持复制名称/路径）、点文件夹行同步左侧、sqlite 持久化（切回回看/大小写/递归分析）
import json
import os
import tempfile
import time
import app as app_mod
from app import App

app = App()
app.withdraw()
here = os.getcwd()  # 用真实大小写的路径，懒加载树按 scandir 大小写逐级查找


def pump_until(pred, rounds=200):
    for _ in range(rounds):
        app.update()
        if pred():
            return True
        time.sleep(0.05)
    return False


def has_total():
    return any('合计' in app.list.item(r)['values'][0] for r in app.list.get_children())


# bug1: 展开 C:\ 必须填充真实子节点，而不是只剩占位符
app._ensure('C:\\')
kids = app.tree.get_children('C:\\')
assert len(kids) > 2, f'C: 子节点异常: {kids}'
assert all(k.startswith('C:\\') for k in kids), kids

# 顶栏导航
app.navigate(here)
assert app.current.endswith('check-folder-sizes'), app.current
assert app.path_var.get().endswith('check-folder-sizes'), app.path_var.get()

# 点击左侧节点 → 顶栏输入框跟随（轮询同步，100ms 内生效）
app.tree.selection_set('D:\\')
assert pump_until(lambda: app.path_var.get() == 'D:\\'), app.path_var.get()

# 分析的是左侧当前选中目录
app.navigate(here)
app.analyze()
ok = pump_until(lambda: str(app.btn['state']) == 'normal' and app.list.get_children())
assert ok and app.list.get_children(), '分析后列表为空'
assert app.btn_var.get() == '分析', app.btn_var.get()
assert '本次扫描' in app.status_var.get(), app.status_var.get()

# bug3: 分析进行中切换目录，结果仍要显示（不许静默丢弃）
app.analyze()
app.set_current('C:\\')  # 模拟扫描期间用户点了别的目录
ok = pump_until(lambda: str(app.btn['state']) == 'normal' and app.list.get_children())
assert ok, '切换目录后结果被丢弃'

# 右键菜单（左侧树）：复制名称 / 复制路径；盘符根节点名称取盘符
app.tree.selection_set('D:\\')
app._copy_tree('name')
assert app.clipboard_get() == 'D:', app.clipboard_get()
app._copy_tree('path')
assert app.clipboard_get() == 'D:\\', app.clipboard_get()
app.tree.selection_set(kids[0])
app._copy_tree('name')
assert app.clipboard_get() == os.path.basename(kids[0]), app.clipboard_get()

# 右键菜单（右侧列表）：复制名称 / 复制路径
rows = app.list.get_children()
folder = next(r for r in rows if app.list.item(r)['values'][2] == '文件夹')
assert app._row_path(folder) == folder
assert app._row_path('total') is None  # 合计行不可复制
app.list.selection_set(folder)
app._copy('name')
assert app.clipboard_get() == os.path.basename(folder), app.clipboard_get()
app._copy('path')
assert app.clipboard_get() == folder, app.clipboard_get()

# 回归：Windows Tk 的 menu add 返回空串（add_command → None），
# 不修的话右键列表时 entryconfig(None) 直接崩 "bad menu entry index -state"
# （真实事件 + menu.post 会阻塞测试事件循环，这里只验证 _on_right_click 崩的那行）
assert app.open_item is not None, '打开文件夹条目索引应由 label 查出'
app.menu.entryconfig(app.open_item, state='disabled')
assert app.menu.entrycget(app.open_item, 'state') == 'disabled'
app.menu.entryconfig(app.open_item, state='normal')

# 右键菜单：打开文件夹（mock 系统调用，不真弹资源管理器）；文件行不打开
opened = []
app._open_folder = lambda p: opened.append(p)
app.list.selection_set(folder)
app._open_from_list()
assert opened == [folder], opened
file_row = next(r for r in app.list.get_children() if app.list.item(r)['values'][2] == '文件')
app.list.selection_set(file_row)
app._open_from_list()
assert opened == [folder], opened
app.tree.selection_set('D:\\')
app._open_from_tree()
assert opened == [folder, 'D:\\'], opened

# 点文件夹行 = 点左侧对应目录：顶栏输入框跟随
app.deiconify()
app.update()
x, y, w, h = app.list.bbox(folder)
app.list.event_generate('<Button-1>', x=x + w // 2, y=y + h // 2)
app.update()
assert app.path_var.get() == folder, app.path_var.get()
assert app.list.get_children(), '点文件夹行后列表应有内容（缓存或无数据提示）'
app.withdraw()

# sqlite 持久化：分析过的目录切走再切回，数据仍在
app.set_current('C:\\')
assert app.path_var.get() == 'C:\\'
app.set_current(here)
assert has_total(), '切回已分析目录后无数据'
assert '缓存' in app.status_var.get(), app.status_var.get()

# 路径大小写不敏感：不同大小写命中同一份缓存
assert app.store.load_items(here.upper()) is not None

# 递归分析：子目录逐层入库，进子目录免重扫也有数据
app.analyze_recursive()
ok = pump_until(lambda: str(app.btn2['state']) == 'normal')
assert ok, '递归分析未结束'
subs = [r for r in app.list.get_children() if app.list.item(r)['values'][2] == '文件夹']
assert subs, '结果里没有文件夹'
hit = [r for r in subs if app.store.load_items(r)]
assert hit, '递归分析后子目录未入库'
app.set_current(hit[0])
assert has_total(), f'进入子目录 {hit[0]} 后无数据'
app.set_current(here)
assert has_total(), '切回原目录后无数据'

# 界面状态持久化：关窗把窗口位置/大小+分隔条比例+上次目录写入 ui_state.json（逻辑像素，DPI 无关）
app_mod.UI_STATE_PATH = os.path.join(tempfile.gettempdir(), 'ui_state_test.json')
if os.path.exists(app_mod.UI_STATE_PATH):
    os.remove(app_mod.UI_STATE_PATH)
app.deiconify()
pump_until(lambda: app.paned.winfo_width() > 100)  # 未映射时 paned 无布局，sash 会夹到 0
app.withdraw()
app._save_ui_state()
st = json.load(open(app_mod.UI_STATE_PATH, encoding='utf-8'))
assert st['geometry']['w'] > 0 and st['geometry']['h'] > 0, st
assert 0 < st['sash'] < 1, st
assert st['dir'] == here, st  # 上次定位的目录（此时 current 是 here）

# 启动定位：恢复上次目录；目录不存在（删了/盘符没插）回退当前目录
assert app_mod.start_dir(st) == here
assert app_mod.start_dir({'dir': 'Z:\\no_such_dir_xyz'}) == os.getcwd()
assert app_mod.start_dir({}) == os.getcwd()

# ============ 专项清理分页 ============
# 夹具：临时目录冒充 LOCALAPPDATA / TEMP（规则在扫描时才读环境变量，不碰真实数据）
tab = app.cleanup_tab
assert app.nb.index('end') == 2, '应有「磁盘分析」「专项清理」两个分页'
assert app.nb.tab(1, 'text') == '专项清理'

fix = tempfile.mkdtemp(prefix='cfs_fix_')
lad = os.path.join(fix, 'LAD')
tmpd = os.path.join(fix, 'Temp')
vs_junk = os.path.join(tmpd, 'abcd1234.efg')  # VS Installer 残留特征：随机名 + vs_installer.exe
os.makedirs(vs_junk)
with open(os.path.join(vs_junk, 'vs_installer.exe'), 'w') as f:
    f.write('v' * 100)
with open(os.path.join(tmpd, 'leftover.tmp'), 'w') as f:
    f.write('t' * 50)
pkg = os.path.join(lad, 'Unity', 'cache', 'packages')
os.makedirs(pkg)
with open(os.path.join(pkg, 'editor.zip'), 'w') as f:
    f.write('u' * 200)
os.makedirs(os.path.join(lad, 'CrashDumps'))
with open(os.path.join(lad, 'CrashDumps', 'app.dmp'), 'w') as f:
    f.write('d' * 80)
sw = os.path.join(lad, 'Microsoft', 'Edge', 'User Data', 'Default',
                  'Service Worker', 'CacheStorage')
os.makedirs(sw)
with open(os.path.join(sw, 'cache.bin'), 'w') as f:
    f.write('e' * 60)

old_lad, old_temp = os.environ.get('LOCALAPPDATA'), os.environ.get('TEMP')
os.environ['LOCALAPPDATA'], os.environ['TEMP'] = lad, tmpd
tab.confirm = lambda *a, **k: True  # 确认框不真弹（会阻塞事件循环）
try:
    # 专项扫描：各规则命中夹具内容，大小正确
    tab.start_scan()
    assert pump_until(lambda: not tab.busy, rounds=400), '专项扫描未结束'
    res = {rid: dict(items) for rid, items in tab.results.items()}
    assert res['temp_vsinstaller'] == {vs_junk: 100}, res['temp_vsinstaller']
    assert res['temp_misc'] == {os.path.join(tmpd, 'leftover.tmp'): 50}, res['temp_misc']
    assert res['unity_cache'] == {pkg: 200}, res['unity_cache']
    assert res['crashdumps'] == {os.path.join(lad, 'CrashDumps', 'app.dmp'): 80}
    assert res['edge_sw'] == {sw: 60}, res['edge_sw']
    assert not res['npm_cache'] and not res['tuanjie_cache'] and not res['pnpm_store']
    assert '可清理' in tab.sum_var1.get(), tab.sum_var1.get()

    # 永久删除模式：安全项被删，未勾选项保留；汇总显示释放量
    tab.recycle_var.set(False)
    tab.check_safe()  # temp_vsinstaller/unity/tuanjie/npm/crashdumps；不含 temp_misc/edge/pnpm
    tab.start_clean()
    assert pump_until(lambda: not tab.busy, rounds=400), '清理未结束'
    assert not os.path.exists(vs_junk), 'VS Installer 残留应被删除'
    assert not os.path.exists(pkg), 'Unity 缓存内容应被删除'
    assert os.path.isdir(os.path.join(lad, 'Unity', 'cache')), '缓存目录本身应保留'
    assert not os.path.exists(os.path.join(lad, 'CrashDumps', 'app.dmp'))
    assert os.path.exists(os.path.join(tmpd, 'leftover.tmp')), 'temp_misc 未勾选不应删'
    assert os.path.exists(sw), 'edge_sw 未勾选不应删'
    assert '本次释放' in tab.sum_var2.get(), tab.sum_var2.get()

    # 进程占用保护：Edge 运行中时 edge_sw 整条跳过
    orig_proc = app_mod.proc_running
    app_mod.proc_running = lambda img: True
    try:
        tab.set_checked(['edge_sw'])
        tab.start_clean()
        assert pump_until(lambda: not tab.busy, rounds=400)
        assert os.path.exists(sw), 'Edge 运行中不应删除其缓存'
        assert '跳过 1' in tab.sum_var2.get(), tab.sum_var2.get()
    finally:
        app_mod.proc_running = orig_proc

    # 删除失败计入失败数，文件保留（proc_running 强制为 False，与真实 Edge 是否运行无关）
    orig_pd = app_mod._perm_delete
    def flaky(p):
        if p.endswith('CacheStorage'):
            raise PermissionError('被占用')
        return orig_pd(p)
    app_mod._perm_delete = flaky
    app_mod.proc_running = lambda img: False
    try:
        tab.start_clean()
        assert pump_until(lambda: not tab.busy, rounds=400)
        assert os.path.exists(sw), '删除失败时文件应保留'
        assert '失败 1' in tab.sum_var2.get(), tab.sum_var2.get()
    finally:
        app_mod._perm_delete = orig_pd
        app_mod.proc_running = orig_proc

    # 回收站模式：走 recycle_paths 通道（测试里 mock 成真实删除，不碰真回收站）
    calls = []
    def fake_recycle(paths):
        calls.append(len(paths))
        for p in paths:
            app_mod._perm_delete(p)
        return list(paths), []
    orig_rec = app_mod.recycle_paths
    app_mod.recycle_paths = fake_recycle
    app_mod.proc_running = lambda img: False
    try:
        tab.recycle_var.set(True)
        tab.set_checked(['edge_sw'])
        tab.start_clean()
        assert pump_until(lambda: not tab.busy, rounds=400)
        assert calls, '回收站模式应调用 recycle_paths'
        assert not os.path.exists(sw), '回收站模式（mock 为真删）后文件应不存在'
    finally:
        app_mod.recycle_paths = orig_rec
        app_mod.proc_running = orig_proc
finally:
    if old_lad is None:
        os.environ.pop('LOCALAPPDATA', None)
    else:
        os.environ['LOCALAPPDATA'] = old_lad
    os.environ['TEMP'] = old_temp
    import shutil as _sh
    _sh.rmtree(fix, ignore_errors=True)

# 清理页状态记忆：勾选集合 + 回收站开关 + 上次分页 都进 ui_state.json
app._save_ui_state()
st2 = json.load(open(app_mod.UI_STATE_PATH, encoding='utf-8'))
assert 'cleanup' in st2 and 'sel' in st2['cleanup'] and 'recycle' in st2['cleanup'], st2
assert st2['tab'] == 0, st2

print('OK —', len(app.list.get_children()), '行')
app.destroy()
