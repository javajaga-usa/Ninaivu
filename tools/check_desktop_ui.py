"""Exercise native widgets with a fake controller; never touches the live server."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import tempfile
import time
import tkinter as tk
from ninaivu.desktop.app import Dashboard

class FakeController:
    mode='standard'
    runtime=Path(tempfile.gettempdir())/'ninaivu-control-ui-test'
    root=Path.cwd()
    def record(self):return None
    def start(self):return 'Test server started'
    def stop(self):return 'Test server stopped'
    def apply_mode(self,mode):self.mode=mode;return 'Applied '+mode
    def server_url(self,admin=False):return 'http://localhost'

root=tk.Tk();dashboard=Dashboard(root,FakeController())
root.update()
for mode in ('performance','power-saving','standard'):
    dashboard.mode.set(mode);dashboard.describe_mode();dashboard.apply_selected_mode()
    until=time.monotonic()+3
    while dashboard.busy and time.monotonic()<until:root.update();time.sleep(.02)
    assert dashboard.controller.mode==mode
root.geometry('820x900');root.update()
assert all(button.winfo_rootx()+button.winfo_width()<=root.winfo_rootx()+root.winfo_width() for button in dashboard.buttons)
assert dashboard.notice.get().startswith('Applied')
assert dashboard.active_mode.get()=='Active mode: Standard'
dashboard.set_controls(False)
assert dashboard.restart_button.instate(['disabled'])
dashboard.set_controls(True)
assert not dashboard.restart_button.instate(['disabled'])
top=dashboard.start_button.winfo_rooty()
dashboard.viewport.yview_moveto(1);root.update()
assert dashboard.start_button.winfo_rooty()==top
dashboard.controller.runtime.mkdir(parents=True,exist_ok=True)
log=dashboard.controller.runtime/'server.log'
log.write_text('Synthetic log line\nERROR synthetic check\n',encoding='utf-8')
dashboard.switch_log();dashboard.poll_logs();root.update()
assert 'Synthetic log line' in dashboard.log_text.get('1.0','end')
dashboard.log_query.set('synthetic');dashboard.find_log()
first=dashboard.log_text.tag_ranges('match')
assert first and not dashboard.log_follow.get()
dashboard.find_log();assert dashboard.log_text.tag_ranges('match')!=first
dashboard.find_log();assert dashboard.log_text.tag_ranges('match')==first
dashboard.log_query.set('missing entry');dashboard.find_log()
assert 'No match' in dashboard.log_info.get()
dashboard.copy_logs();assert 'Synthetic log line' in root.clipboard_get()
dashboard.log_paused.set(True)
with log.open('a',encoding='utf-8') as stream:stream.write('Paused append\n')
dashboard.poll_logs();assert 'Paused append' not in dashboard.log_text.get('1.0','end')
dashboard.log_paused.set(False);dashboard.poll_logs()
assert 'Paused append' in dashboard.log_text.get('1.0','end')
dashboard.clear_log_view();assert not dashboard.log_text.get('1.0','end').strip()
print('Native UI: three mode actions, responsive worker handoff and minimum-width controls passed.')
print('Embedded logs: append, pause, resume and clear-view passed.')
dashboard.close()
