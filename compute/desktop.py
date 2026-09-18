#!/usr/bin/env python3
"""Native GTK/WebKit window for the private local cluster console."""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.request

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('WebKit2', '4.1')
from gi.repository import Gio, GLib, Gtk, WebKit2

BASE = 'http://127.0.0.1:18891'


class ClusterApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id='io.local.ClusterDesk', flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.window = None
        self.view = None

    def do_activate(self):
        if self.window:
            self.window.present()
            return
        self.window = Gtk.ApplicationWindow(application=self, title='Cluster Desk')
        self.window.set_default_size(1440, 920)
        self.window.set_size_request(900, 600)
        self.window.set_wmclass('cluster-desk', 'ClusterDesk')
        icon = Path(__file__).with_name('desktop') / 'icon.svg'
        if icon.exists():
            self.window.set_icon_from_file(str(icon))
        self.container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.loading = Gtk.Label(label='Connecting to your cluster…')
        self.container.pack_start(self.loading, True, True, 0)
        self.window.add(self.container)
        self.window.connect('destroy', lambda *_: self.quit())
        self.window.show_all()
        threading.Thread(target=self.start_console, daemon=True).start()

    def start_console(self):
        try:
            subprocess.run(['systemctl', '--user', 'start', 'compute-cluster-console.service'],
                           capture_output=True, timeout=12, check=True)
            for _ in range(30):
                try:
                    with urllib.request.urlopen(BASE + '/', timeout=1) as response:
                        if response.status == 200:
                            GLib.idle_add(self.show_console)
                            return
                except OSError:
                    time.sleep(.3)
            raise RuntimeError('The local control service did not respond.')
        except (OSError, RuntimeError, subprocess.SubprocessError):
            GLib.idle_add(self.start_failed)

    def start_failed(self):
        self.loading.set_text('The cluster control app could not start.\nCheck the compute-cluster-console user service, then reopen Cluster Desk.')
        return False

    def show_console(self):
        if self.view:
            return False
        private = Path.home() / '.local/state/compute-cluster/desktop-webview'
        private.mkdir(parents=True, exist_ok=True, mode=0o700)
        manager = WebKit2.WebsiteDataManager(base_data_directory=str(private / 'data'),
                                            base_cache_directory=str(private / 'cache'))
        context = WebKit2.WebContext.new_with_website_data_manager(manager)
        context.set_sandbox_enabled(True)
        content = WebKit2.UserContentManager()
        content.register_script_message_handler('clusterNative')
        content.connect('script-message-received::clusterNative', self.native_message)
        self.view = WebKit2.WebView(web_context=context, user_content_manager=content)
        settings = self.view.get_settings()
        settings.set_enable_developer_extras(False)
        settings.set_allow_file_access_from_file_urls(False)
        settings.set_allow_universal_access_from_file_urls(False)
        self.view.connect('decide-policy', self.decide_policy)
        self.view.connect('permission-request', lambda _v, request: (request.deny(), True)[1])
        context.connect('download-started', self.download_started)
        self.container.remove(self.loading)
        self.container.pack_start(self.view, True, True, 0)
        self.view.load_uri(BASE + '/')
        self.window.show_all()
        return False

    def decide_policy(self, _view, decision, kind):
        if kind in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION, WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            action = decision.get_navigation_action()
            uri = action.get_request().get_uri()
            if uri == BASE or uri.startswith(BASE + '/') or uri.startswith('blob:' + BASE + '/'):
                if kind == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
                    self.view.load_uri(uri)
                    decision.ignore()
                    return True
                return False
            if action.get_navigation_type() == WebKit2.NavigationType.LINK_CLICKED and uri.startswith(('https://', 'http://')):
                Gtk.show_uri_on_window(self.window, uri, Gtk.get_current_event_time())
            decision.ignore()
            return True
        if kind == WebKit2.PolicyDecisionType.RESPONSE and not decision.is_mime_type_supported():
            decision.download()
            return True
        return False

    def native_message(self, _manager, message):
        if not self.view.get_uri().startswith(BASE + '/'):
            return
        try:
            payload = json.loads(message.get_js_value().to_string())
        except (ValueError, AttributeError):
            return
        if not isinstance(payload, dict) or payload.get('action') != 'choose_folder':
            return
        dialog = Gtk.FileChooserDialog(title='Choose a project folder', parent=self.window,
                                       action=Gtk.FileChooserAction.SELECT_FOLDER)
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Choose folder', Gtk.ResponseType.OK)
        path = dialog.get_filename() if dialog.run() == Gtk.ResponseType.OK else None
        dialog.destroy()
        self.view.run_javascript('window.clusterFolderSelected && window.clusterFolderSelected(' + json.dumps(path) + ');', None, None, None)

    def download_started(self, _context, download):
        uri = download.get_request().get_uri()
        if not (uri.startswith(BASE + '/') or uri.startswith('blob:' + BASE + '/')):
            download.cancel()
            return
        download.connect('decide-destination', self.download_destination)

    def download_destination(self, download, suggested):
        dialog = Gtk.FileChooserDialog(title='Save cluster patch', parent=self.window,
                                       action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Save patch', Gtk.ResponseType.OK)
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_current_name(Path(suggested or 'cluster-changes.patch').name)
        folder = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
        if folder and Path(folder).is_dir():
            dialog.set_current_folder(folder)
        accepted = dialog.run() == Gtk.ResponseType.OK
        destination = dialog.get_filename() if accepted else None
        dialog.destroy()
        if destination:
            download.set_allow_overwrite(True)
            download.set_destination(Path(destination).as_uri())
        else:
            download.cancel()
        return True


if __name__ == '__main__':
    sys.exit(ClusterApp().run(sys.argv))
