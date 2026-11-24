#!/usr/bin/env python3
"""Script Studio — GTK4/libadwaita batch launcher for local shell scripts."""
import os
import json
import stat
import shutil
import signal
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gio, GLib, Gdk

APP_ID = 'org.example.ScriptStudio'
CONFIG = Path.home() / '.config' / 'script-studio' / 'settings.json'
LOG_DIR = Path.home() / '.local' / 'state' / 'script-studio' / 'logs'

VERSION = '1.0.1'
AUTHOR = 'Joshua Dwight'


class Studio(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.connect('activate', self.activate)
        self.folder = Path.home()
        self.rows = {}
        self.queue = []
        self.busy = False
        self.cancel_event = threading.Event()
        self.active_process = None
        self.run_log = None
        self.log_lock = threading.Lock()
        self.load_config()

    def load_config(self):
        try:
            data = json.loads(CONFIG.read_text())
            directory = Path(data.get('folder', '')).expanduser()
            if directory.is_dir():
                self.folder = directory
        except (OSError, ValueError):
            pass

    def save_config(self):
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps({'folder': str(self.folder)}, indent=2))

    def activate(self, *_):
        if self.get_active_window():
            self.get_active_window().present()
            return
        self.win = Adw.ApplicationWindow(application=self, title='Script Studio', default_width=1120, default_height=780)
        self.win.set_icon_name(APP_ID)
        self.win.set_size_request(760, 500)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        branding = Gtk.Box(spacing=8)
        logo = self.icon(APP_ID)
        logo.set_pixel_size(26)
        branding.append(logo)
        branding.append(Adw.WindowTitle(title='Script Studio', subtitle=f'{AUTHOR} · v{VERSION}'))
        header.set_title_widget(branding)
        menu = Gio.Menu()
        menu.append('Choose Folder…', 'app.choose-folder')
        menu.append('Install Desktop Shortcut', 'app.install-shortcut')
        menu.append('Install in Application Menu', 'app.install-app-menu')
        for name, callback in [('choose-folder', self.choose_folder), ('install-shortcut', self.install_shortcut),
                               ('install-app-menu', self.install_app_menu)]:
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', callback)
            self.add_action(action)
        header.pack_start(Gtk.MenuButton(label='File', menu_model=menu))
        help_menu = Gio.Menu()
        help_menu.append('User Guide', 'app.user-guide')
        guide = Gio.SimpleAction.new('user-guide', None)
        guide.connect('activate', self.show_user_guide)
        self.add_action(guide)
        help_menu.append('About', 'app.about')
        about = Gio.SimpleAction.new('about', None)
        about.connect('activate', self.show_about)
        self.add_action(about)
        header.pack_start(Gtk.MenuButton(label='Help', menu_model=help_menu))
        toolbar.add_top_bar(header)
        self.win.set_content(toolbar)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_start(22); outer.set_margin_end(22)
        outer.set_margin_top(16); outer.set_margin_bottom(16)
        toolbar.set_content(outer)

        folder_bar = Gtk.Box(spacing=10)
        folder_bar.add_css_class('card')
        folder_bar.set_margin_bottom(3)
        folder_bar.set_margin_top(2)
        folder_bar.set_margin_start(2)
        folder_bar.set_margin_end(2)
        folder_bar.append(self.icon('folder-symbolic'))
        self.path_label = Gtk.Label(xalign=0, ellipsize=3, hexpand=True, selectable=True)
        folder_bar.append(self.path_label)
        self.folder_button = self.button('Choose Folder', 'folder-open-symbolic', self.choose_folder)
        folder_bar.append(self.folder_button)
        folder_bar.append(self.button('Refresh', 'view-refresh-symbolic', lambda *_: self.scan()))
        outer.append(folder_bar)

        actions = Gtk.Box(spacing=8)
        self.select_all = self.button('Select All', 'object-select-symbolic', self.select_all_files)
        actions.append(self.select_all)
        actions.append(self.button('Clear Selection', 'edit-clear-symbolic', self.clear_selection))
        actions.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.chmod_button = self.button('Make Executable', 'emblem-ok-symbolic', self.make_executable)
        actions.append(self.chmod_button)
        self.run_button = self.button('Run Selected', 'media-playback-start-symbolic', self.confirm_run)
        self.run_button.add_css_class('suggested-action')
        actions.append(self.run_button)
        self.stop_button = self.button('Stop Queue', 'media-playback-stop-symbolic', self.stop_queue)
        self.stop_button.set_sensitive(False)
        actions.append(self.stop_button)
        self.root_switch = Gtk.Switch(active=False, valign=Gtk.Align.CENTER)
        root_box = Gtk.Box(spacing=5, hexpand=True, halign=Gtk.Align.END)
        root_box.append(Gtk.Label(label='Run as administrator'))
        root_box.append(self.root_switch)
        actions.append(root_box)
        outer.append(actions)

        search_row = Gtk.Box(spacing=10)
        self.search = Gtk.SearchEntry(placeholder_text='Filter scripts by name…', hexpand=True)
        self.search.connect('search-changed', lambda *_: self.filter_rows())
        search_row.append(self.search)
        self.count_label = Gtk.Label(label='0 scripts')
        self.count_label.add_css_class('dim-label')
        search_row.append(self.count_label)
        outer.append(search_row)

        panes = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        panes.set_position(350)
        outer.append(panes)

        scripts_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scripts_card.add_css_class('card')
        heading = Gtk.Box(spacing=16)
        heading.set_margin_top(11); heading.set_margin_bottom(11)
        heading.set_margin_start(16); heading.set_margin_end(16)
        heading.append(Gtk.Label(label='SCRIPTS', xalign=0, hexpand=True, css_classes=['caption', 'dim-label']))
        heading.append(Gtk.Label(label='PERMISSION / LAST RESULT', css_classes=['caption', 'dim-label']))
        scripts_card.append(heading)
        scripts_card.append(Gtk.Separator())
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=['boxed-list'])
        self.listbox.set_margin_top(5)
        scroller.set_child(self.listbox)
        scripts_card.append(scroller)
        panes.set_start_child(scripts_card)

        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        bottom.set_margin_top(14)
        log_header = Gtk.Box(spacing=8)
        log_header.append(Gtk.Label(label='Execution Console', xalign=0, hexpand=True, css_classes=['heading']))
        self.queue_label = Gtk.Label(label='Idle', css_classes=['dim-label'])
        log_header.append(self.queue_label)
        log_header.append(self.button('Clear', 'edit-clear-symbolic', lambda *_: self.buffer.set_text('')))
        log_header.append(self.button('Open Logs', 'folder-open-symbolic', self.open_logs))
        bottom.append(log_header)
        console_scroll = Gtk.ScrolledWindow(vexpand=True)
        console_scroll.add_css_class('card')
        self.console = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.CHAR, left_margin=12, right_margin=12, top_margin=12, bottom_margin=12)
        self.buffer = self.console.get_buffer()
        console_scroll.set_child(self.console)
        bottom.append(console_scroll)
        self.progress = Gtk.ProgressBar(show_text=False)
        bottom.append(self.progress)
        panes.set_end_child(bottom)
        self.status = Gtk.Label(label='Ready', xalign=0, css_classes=['dim-label', 'caption'])
        outer.append(self.status)

        css = Gtk.CssProvider()
        css.load_from_data(b'''
            .card { border-radius: 12px; }
            .script-row { padding: 6px 12px; }
            .status-good { color: #2ec27e; font-weight: 600; }
            .status-bad { color: #f66151; font-weight: 600; }
            .status-neutral { opacity: .75; }
            .status-running { color: #f5c211; font-weight: 600; }
        ''')
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.scan()
        self.win.present()

    def show_user_guide(self, *_):
        try:
            text = Path(__file__).with_name('USER-GUIDE.txt').read_text(encoding='utf-8')
        except OSError as error:
            self.alert('Could not open the user guide', str(error))
            return
        window = Gtk.Window(
            title='Script Studio User Guide', transient_for=self.win, modal=True,
            destroy_with_parent=True, default_width=720, default_height=600,
        )
        view = Gtk.TextView(
            editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR,
            left_margin=24, right_margin=24, top_margin=20, bottom_margin=20,
            pixels_below_lines=4,
        )
        view.get_buffer().set_text(text)
        window.set_child(Gtk.ScrolledWindow(child=view))
        window.present()

    def show_about(self, *_):
        about = Adw.AboutWindow(
            transient_for=self.win, modal=True, application_name='Script Studio',
            application_icon=self.get_application_id(), version=VERSION,
            developer_name=AUTHOR, developers=[AUTHOR], comments='Manage and run local shell scripts.',
        )
        about.present()

    def icon(self, name):
        return Gtk.Image.new_from_icon_name(name)

    def button(self, text, icon, callback):
        btn = Gtk.Button(label=text)
        if icon: btn.set_icon_name(icon)
        btn.connect('clicked', callback)
        return btn

    def install_app_menu(self, *_):
        app_id = self.get_application_id()
        script = Path(__file__).resolve()
        icon_file = script.with_name(f'{app_id}.svg')
        folder = Path(GLib.get_user_data_dir()) / 'applications'
        target = folder / 'script-studio.desktop'
        escaped_script = str(script).replace('%', '%%')
        for character in ('\\', '"', '`', '$'):
            escaped_script = escaped_script.replace(character, '\\' + character)

        entry = GLib.KeyFile()
        properties = {
            'Type': 'Application',
            'Name': self.win.get_title(),
            'Comment': 'Batch manage and run Ubuntu shell scripts',
            'Exec': f'/usr/bin/python3 "{escaped_script}"',
            'Icon': str(icon_file) if icon_file.is_file() else app_id,
            'StartupWMClass': app_id,
            'Categories': 'Utility;',
        }
        for key, value in properties.items():
            entry.set_string('Desktop Entry', key, value)
        entry.set_boolean('Desktop Entry', 'Terminal', False)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            data, _ = entry.to_data()
            Gio.File.new_for_path(str(target)).replace_contents(
                data.encode('utf-8'), None, False, Gio.FileCreateFlags.REPLACE_DESTINATION, None,
            )
        except (OSError, GLib.Error) as error:
            self.alert('Could not install application launcher', str(error))
            return
        self.alert('Application launcher installed', f'{self.win.get_title()} is registered in your application menu.')

    def install_shortcut(self, *_):
        desktop = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DESKTOP)
        home = Path.home()
        folder = Path(desktop) if desktop and Path(desktop) != home else home / 'Desktop'
        target = folder / 'script-studio.desktop'
        launcher = Path(GLib.get_user_data_dir()) / 'applications' / target.name
        shortcut = Gio.File.new_for_path(str(target))
        try:
            entry = launcher.read_bytes()
            folder.mkdir(parents=True, exist_ok=True)
            shortcut.replace_contents(entry, None, False, Gio.FileCreateFlags.REPLACE_DESTINATION, None)
            target.chmod(0o755)
        except (OSError, GLib.Error) as exc:
            self.alert('Could not install shortcut', str(exc))
            return
        try:
            shortcut.set_attribute_string('metadata::trusted', 'true', Gio.FileQueryInfoFlags.NONE, None)
        except GLib.Error:
            self.alert('Shortcut installed', f'{target}\nIf prompted, right-click it and choose Allow Launching.')
        else:
            self.alert('Shortcut installed', str(target))

    def choose_folder(self, *_):
        dialog = Gtk.FileDialog(title='Choose folder containing shell scripts')
        dialog.set_initial_folder(Gio.File.new_for_path(str(self.folder)))
        dialog.select_folder(self.win, None, self.folder_chosen)

    def folder_chosen(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
            path = folder.get_path()
            if path:
                self.folder = Path(path)
                self.save_config()
                self.scan()
        except GLib.Error:
            pass

    def scan(self):
        if self.busy:
            return
        self.rows.clear()
        while child := self.listbox.get_first_child():
            self.listbox.remove(child)
        self.path_label.set_text(str(self.folder))
        try:
            paths = sorted((p for p in self.folder.iterdir() if p.is_file() and p.suffix.lower() == '.sh' and not p.is_symlink()), key=lambda p: p.name.lower())
        except OSError as e:
            self.alert('Unable to read folder', str(e))
            paths = []
        for path in paths:
            row = Gtk.ListBoxRow()
            row.add_css_class('script-row')
            content = Gtk.Box(spacing=12, valign=Gtk.Align.CENTER)
            check = Gtk.CheckButton()
            content.append(check)
            content.append(self.icon('text-x-script-symbolic'))
            name_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
            name = Gtk.Label(label=path.name, xalign=0, ellipsize=3)
            name_box.append(name)
            try:
                info = path.stat()
                details = f'{info.st_size:,} bytes  •  {datetime.fromtimestamp(info.st_mtime):%Y-%m-%d %H:%M}'
            except OSError:
                details = 'File unavailable'
            name_box.append(Gtk.Label(label=details, xalign=0, css_classes=['caption', 'dim-label']))
            content.append(name_box)
            badge = Gtk.Label(xalign=1)
            content.append(badge)
            row.set_child(content)
            self.listbox.append(row)
            self.rows[path] = (row, check, badge)
            self.set_badge(path, 'Executable' if os.access(path, os.X_OK) else 'Not executable', 'good' if os.access(path, os.X_OK) else 'neutral')
        self.filter_rows()

    def filter_rows(self):
        query = self.search.get_text().strip().lower()
        count = 0
        for path, (row, _, _) in self.rows.items():
            visible = query in path.name.lower()
            row.set_visible(visible)
            count += visible
        self.count_label.set_text(f'{count} scripts')
        self.status.set_text(f'{len(self.rows)} .sh files in folder • subfolders excluded • symlinks excluded')

    def selected(self):
        # Selection order matches the visible sorted list.
        return [p for p, (row, checkbox, _) in self.rows.items() if row.get_visible() and checkbox.get_active()]

    def select_all_files(self, *_):
        for row, check, _ in self.rows.values():
            if row.get_visible(): check.set_active(True)

    def clear_selection(self, *_):
        for _, check, _ in self.rows.values():
            check.set_active(False)

    def set_badge(self, path, value, style='neutral'):
        if path not in self.rows: return False
        badge = self.rows[path][2]
        for css in ('status-good', 'status-bad', 'status-neutral', 'status-running'):
            badge.remove_css_class(css)
        badge.set_text(('● ' if style in ('good','bad','running') else '○ ') + value)
        badge.add_css_class('status-' + style)
        return False

    def alert(self, title, body):
        dialog = Adw.MessageDialog(transient_for=self.win, heading=title, body=body)
        dialog.add_response('ok', 'OK')
        dialog.present()

    def make_executable(self, *_):
        paths = self.selected()
        if not paths:
            self.alert('No scripts selected', 'Select one or more scripts first.')
            return
        dialog = Adw.MessageDialog(transient_for=self.win, heading='Make scripts executable?', body=f'Add execute permissions to {len(paths)} selected script(s)? Files you do not own may require administrator authentication.')
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('confirm', 'Make Executable')
        dialog.set_response_appearance('confirm', Adw.ResponseAppearance.SUGGESTED)
        dialog.connect('response', lambda _d, r: self.start_jobs('chmod', paths, False) if r == 'confirm' else None)
        dialog.present()

    def confirm_run(self, *_):
        paths = self.selected()
        if not paths:
            self.alert('No scripts selected', 'Select one or more scripts to run.')
            return
        root = self.root_switch.get_active()
        names = '\n'.join('• ' + p.name for p in paths[:10])
        if len(paths) > 10: names += f'\n… and {len(paths)-10} more'
        detail = f'Scripts will run ONE AT A TIME in the displayed order.\n\n{names}\n\n'
        detail += 'Administrator authentication will be requested for each script. ' if root else 'Scripts run under your user account (not root). '
        detail += 'Only execute scripts you have reviewed and trust.'
        dialog = Adw.MessageDialog(transient_for=self.win, heading=f'Run {len(paths)} script(s)?', body=detail)
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('run', 'Run Queue')
        dialog.set_response_appearance('run', Adw.ResponseAppearance.DESTRUCTIVE if root else Adw.ResponseAppearance.SUGGESTED)
        dialog.connect('response', lambda _d, r: self.start_jobs('run', paths, root) if r == 'run' else None)
        dialog.present()

    def start_jobs(self, operation, paths, root):
        if self.busy: return
        self.busy = True
        self.cancel_event.clear()
        self.run_button.set_sensitive(False)
        self.chmod_button.set_sensitive(False)
        self.folder_button.set_sensitive(False)
        self.stop_button.set_sensitive(True)
        self.queue = list(paths)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.run_log = LOG_DIR / f'{datetime.now():%Y%m%d-%H%M%S}-{operation}.log'
        self.append_log(f'--- {operation.upper()} SESSION | {datetime.now():%Y-%m-%d %H:%M:%S} ---\nFolder: {self.folder}\nItems: {len(paths)}\nElevation: {root}\n\n')
        threading.Thread(target=self.worker, args=(operation, paths, root), daemon=True).start()

    def worker(self, operation, paths, root):
        successes = 0
        failures = 0
        for index, path in enumerate(paths, 1):
            if self.cancel_event.is_set(): break
            GLib.idle_add(self.queue_label.set_text, f'{index} / {len(paths)}')
            GLib.idle_add(self.progress.set_fraction, (index - 1) / len(paths))
            GLib.idle_add(self.set_badge, path, 'Working…', 'running')
            # Check each path again: no symlinks and no directory escape.
            try:
                if path.is_symlink() or not path.is_file() or path.parent.resolve() != self.folder.resolve():
                    raise ValueError('File is missing, linked, or outside the selected folder')
                if operation == 'chmod':
                    mode = path.stat().st_mode
                    # Set execute where read permission already exists, respecting owner/group/other.
                    needed = ((mode & stat.S_IRUSR and stat.S_IXUSR) or 0) | ((mode & stat.S_IRGRP and stat.S_IXGRP) or 0) | ((mode & stat.S_IROTH and stat.S_IXOTH) or 0)
                    desired = mode | needed
                    if desired != mode:
                        try:
                            os.chmod(path, desired)
                            code = 0
                        except PermissionError:
                            if not shutil.which('pkexec'):
                                raise RuntimeError('pkexec is not installed')
                            code = self.process(['pkexec', '/usr/bin/chmod', f'{desired & 0o7777:o}', '--', str(path)], self.folder)
                    else:
                        code = 0
                else:
                    if not os.access(path, os.R_OK):
                        raise RuntimeError('Script is not readable by the current user')
                    # Run bash explicitly: no implicit sudo and no shell command string.
                    command = ['/usr/bin/bash', str(path)]
                    if root:
                        if not shutil.which('pkexec'):
                            raise RuntimeError('pkexec is not installed')
                        command = ['pkexec', '/usr/bin/bash', str(path)]
                    code = self.process(command, self.folder)
                if code == 0:
                    successes += 1
                    GLib.idle_add(self.set_badge, path, 'Executable' if operation == 'chmod' else 'Success', 'good')
                else:
                    failures += 1
                    reason = 'Cancelled' if self.cancel_event.is_set() else f'Failed (exit {code})'
                    GLib.idle_add(self.set_badge, path, reason, 'bad')
            except Exception as exc:
                failures += 1
                self.append_log(f'ERROR {path.name}: {exc}\n')
                GLib.idle_add(self.set_badge, path, 'Failed', 'bad')
            GLib.idle_add(self.progress.set_fraction, index / len(paths))
        cancelled = self.cancel_event.is_set()
        self.append_log(f'\n--- COMPLETE: {successes} succeeded, {failures} failed' + (', queue stopped' if cancelled else '') + ' ---\n')
        GLib.idle_add(self.finish_jobs, successes, failures, cancelled)

    def process(self, cmd, cwd):
        self.append_log(f'\n$ {" ".join(cmd)}\n')
        # Output is line-buffered into the GUI and stored in a persistent session log.
        try:
            process = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace', bufsize=1, start_new_session=True)
        except OSError as exc:
            self.append_log(f'Launch error: {exc}\n')
            return 127
        self.active_process = process
        try:
            for line in iter(process.stdout.readline, ''):
                self.append_log(line)
            return process.wait()
        finally:
            self.active_process = None
            if process.stdout: process.stdout.close()

    def append_log(self, text):
        if self.run_log:
            try:
                with self.log_lock:
                    with self.run_log.open('a', encoding='utf-8') as f:
                        f.write(text)
            except OSError:
                pass
        GLib.idle_add(self.display_log, text)

    def display_log(self, text):
        end = self.buffer.get_end_iter()
        self.buffer.insert(end, text)
        self.console.scroll_to_iter(self.buffer.get_end_iter(), 0, False, 0, 0)
        return False

    def stop_queue(self, *_):
        self.cancel_event.set()
        self.stop_button.set_sensitive(False)
        self.append_log('\nStop requested. Terminating current script process group…\n')
        process = self.active_process
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        # Important: elevated grandchildren may survive; users must review in-flight system changes.

    def finish_jobs(self, successes, failures, cancelled):
        self.busy = False
        self.run_button.set_sensitive(True)
        self.chmod_button.set_sensitive(True)
        self.folder_button.set_sensitive(True)
        self.stop_button.set_sensitive(False)
        self.queue_label.set_text('Stopped' if cancelled else 'Completed')
        self.status.set_text(f'Success: {successes}  •  Failed: {failures}  •  Log: {self.run_log}')
        return False

    def open_logs(self, *_):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        Gio.AppInfo.launch_default_for_uri(LOG_DIR.as_uri(), None)

if __name__ == '__main__':
    Studio().run()
