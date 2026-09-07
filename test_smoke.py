# 端到端冒烟测试：展开 C:\、顶栏导航与同步、分析按钮、分析期间切换目录不丢结果
import time
from app import App

app = App()
app.withdraw()


def pump_until(pred, rounds=200):
    for _ in range(rounds):
        app.update()
        if pred():
            return True
        time.sleep(0.05)
    return False


# bug1: 展开 C:\ 必须填充真实子节点，而不是只剩占位符
app._ensure('C:\\')
kids = app.tree.get_children('C:\\')
assert len(kids) > 2, f'C: 子节点异常: {kids}'
assert all(k.startswith('C:\\') for k in kids), kids

# 顶栏导航
app.navigate(r'D:\Project\check-folder-sizes')
assert app.current.endswith('check-folder-sizes'), app.current
assert app.path_var.get().endswith('check-folder-sizes'), app.path_var.get()

# 点击左侧节点 → 顶栏输入框跟随（轮询同步，100ms 内生效）
app.tree.selection_set('D:\\')
assert pump_until(lambda: app.path_var.get() == 'D:\\'), app.path_var.get()

# 分析的是左侧当前选中目录
app.navigate(r'D:\Project\check-folder-sizes')
app.analyze()
ok = pump_until(lambda: str(app.btn['state']) == 'normal' and app.list.get_children())
assert ok and app.list.get_children(), '分析后列表为空'
assert app.btn_var.get() == '分析', app.btn_var.get()

# bug3: 分析进行中切换目录，结果仍要显示（不许静默丢弃）
app.analyze()
app.set_current('C:\\')  # 模拟扫描期间用户点了别的目录
ok = pump_until(lambda: str(app.btn['state']) == 'normal' and app.list.get_children())
assert ok, '切换目录后结果被丢弃'

print('OK —', len(app.list.get_children()), '行')
app.destroy()
