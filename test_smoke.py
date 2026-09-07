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

print('OK —', len(app.list.get_children()), '行')
app.destroy()
