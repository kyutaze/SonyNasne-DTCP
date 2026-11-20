#!/usr/bin/env python3
"""Tkinter GUI for the DTCP-IP rescue tool (stdlib only, Win/Linux/macOS).

Features (kept minimal on purpose):
  - connection check button: fetch the device description from the
    manual address (or run SSDP discovery) and show whether the
    recorder answered, with the reason when it did not
  - list recordings on the recorder (no credentials needed)
  - fetch selected recordings via pydtcpip `stream` (see rescue.py)
  - progress bar (watches the .partial file size) with the transfer
    rate in decimal MB/s, and a log pane
  - manifest view with SHA-256 re-verification
  - credentials.env path display and placement status
  - manual recorder address (skips SSDP discovery, for routed paths
    such as VPN / subnet routing); the value is kept between runs

The GUI follows the same rule as the CLI: it NEVER reads, prints, or
verifies credential values. Placement status is inferred from file
metadata only (existence, and whether the size differs from the
bundled empty template); the credentials file itself is never opened
here.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import unicodedata
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rescue  # noqa: E402

TOOL_DIR = Path(__file__).resolve().parent
TEMPLATE = TOOL_DIR / 'credentials.env.example'
SETTINGS_FILE = 'gui_settings.json'


def load_saved_host(config: rescue.Config) -> str | None:
    """Recorder address saved by a previous GUI run.

    The settings file is runtime state kept in the data directory
    (next to pydtcpip's srm.bin); it holds no credentials.
    """
    try:
        data = json.loads((config.data_dir / SETTINGS_FILE).read_text(encoding='utf-8'))
    except Exception:
        return None
    host = data.get('host')
    return host if isinstance(host, str) and host else None


def save_host(config: rescue.Config, host: str | None) -> None:
    """Persist the manual recorder address for the next run."""
    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        (config.data_dir / SETTINGS_FILE).write_text(
            json.dumps({'host': host or ''}, ensure_ascii=False) + '\n',
            encoding='utf-8',
        )
    except Exception:
        pass  # persistence is a convenience; never block the workflow


def credentials_status(config: rescue.Config) -> str:
    """Placement status from metadata only (the file is never opened).

    A placed file holds two hex values (216 characters in total), so its
    size always differs from the bundled empty template: a hand-written
    minimal file (just the two assignment lines) is *smaller* than the
    comment-heavy template, and a filled-in copy of the template is
    larger. Only an untouched copy of the template has exactly the
    template's size. (An earlier version treated "not larger than the
    template" as unplaced, which misreported hand-written files on a
    fresh data directory -- found on a real Mac, 2026-11-17.)
    """
    path = config.credentials
    if not path.exists():
        return '未配置 (ファイルがありません)'
    size = path.stat().st_size
    if size == 0:
        return '未配置 (空ファイルです)'
    if TEMPLATE.exists() and size == TEMPLATE.stat().st_size:
        return '未配置 (空テンプレートのまま)'
    return '配置済み (ファイルサイズからの推定)'


def format_bytes(size: int | None) -> str:
    if size is None:
        return '-'
    value = float(size)
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if value < 1024 or unit == 'GiB':
            return f'{value:.1f} {unit}' if unit != 'B' else f'{int(value)} B'
        value /= 1024
    return f'{value:.1f} GiB'


def original_size(recording: dict) -> int | None:
    for resource in recording.get('resources', []):
        if resource.get('is_original'):
            return resource.get('cleartext_size') or resource.get('size')
    return None


def text_key(value: object) -> str | None:
    """Sort key for text: width-normalised and case-insensitive.

    Ordering is by character code (no reading-based kana order). Empty
    values return None so they are placed last in either direction.
    """
    if value is None:
        return None
    text = unicodedata.normalize('NFKC', str(value)).casefold()
    return text or None


def number_key(value: object) -> int | float | None:
    """Sort key for numbers; anything that is not a number is "no value"."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def duration_key(value: object) -> int | None:
    """Seconds from an ``HH:MM:SS`` string (``MM:SS`` is accepted too)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parts = [int(part) for part in value.strip().split(':')]
    except ValueError:
        return None
    if not 2 <= len(parts) <= 3 or any(part < 0 for part in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


class SortableTree:
    """Click-the-header sorting for a ttk.Treeview.

    Cells hold display strings (``"1.5 GiB"``), which do not sort
    meaningfully, so each row also gets raw sort keys via ``set_keys``.
    Rows are reordered with ``Treeview.move`` -- never deleted and
    re-created -- so item ids and the current selection are kept.
    Rows whose key is None always go last, in either direction. The
    chosen column / direction survive a rebuild of the table: call
    ``clear`` before re-inserting rows and ``apply`` afterwards.
    """

    def __init__(
        self,
        tree: ttk.Treeview,
        labels: dict[str, str],
        column: str | None = None,
        reverse: bool = False,
    ) -> None:
        self.tree = tree
        self.labels = labels
        self.column = column
        self.reverse = reverse
        self.keys: dict[str, dict[str, object]] = {}
        for name in labels:
            tree.heading(name, command=lambda name=name: self.sort_by(name))
        self._update_headings()

    def clear(self) -> None:
        self.keys.clear()

    def set_keys(self, iid: str, keys: dict[str, object]) -> None:
        self.keys[iid] = keys

    def sort_by(self, column: str) -> None:
        """Header click: same column toggles direction, a new one starts ascending."""
        if column == self.column:
            self.reverse = not self.reverse
        else:
            self.column = column
            self.reverse = False
        self.apply()

    def apply(self) -> None:
        if self.column is not None:
            column = self.column
            iids = list(self.tree.get_children(''))
            present = [i for i in iids if self.keys.get(i, {}).get(column) is not None]
            missing = [i for i in iids if self.keys.get(i, {}).get(column) is None]
            present.sort(key=lambda i: self.keys[i][column], reverse=self.reverse)
            for index, iid in enumerate(present + missing):
                self.tree.move(iid, '', index)
        self._update_headings()

    def _update_headings(self) -> None:
        for name, label in self.labels.items():
            if name == self.column:
                label += ' ▼' if self.reverse else ' ▲'
            self.tree.heading(name, text=label)


class RescueApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title('DTCP-IP 録画救出ツール')
        self.geometry('980x680')
        self.config = rescue.resolve_config()
        if self.config.host is None:
            saved_host = load_saved_host(self.config)
            if saved_host:
                self.config = rescue.resolve_config(host=saved_host)
        self.events: queue.Queue = queue.Queue()
        self.listing: dict | None = None
        self.recordings: dict[str, dict] = {}
        self.fetching = False
        self.connecting = False
        self.active_partial: Path | None = None
        self.active_expected: int | None = None
        self.fetch_started_at: float | None = None
        self.progress_samples: list[tuple[float, int]] = []

        self._build_widgets()
        self._refresh_credentials_status()
        self._refresh_manifest()
        self.after(120, self._drain_events)
        self.after(300, self._poll_progress)

    # ------------------------------------------------------------------
    # widgets
    # ------------------------------------------------------------------
    def _build_widgets(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill='x')

        ttk.Label(top, text='資格情報ファイル (credentials.env):').grid(
            row=0, column=0, sticky='w'
        )
        self.credentials_var = tk.StringVar(value=str(self.config.credentials))
        entry = ttk.Entry(top, textvariable=self.credentials_var, width=80)
        entry.grid(row=0, column=1, sticky='ew', padx=4)
        ttk.Button(top, text='変更…', command=self._choose_credentials).grid(
            row=0, column=2
        )
        ttk.Button(top, text='フォルダを開く', command=self._open_credentials_folder).grid(
            row=0, column=3, padx=(4, 0)
        )
        ttk.Label(top, text='nasne アドレス (手動指定):').grid(
            row=1, column=0, sticky='w', pady=(4, 0)
        )
        self.host_var = tk.StringVar(value=self.config.host or '')
        host_entry = ttk.Entry(top, textvariable=self.host_var, width=80)
        host_entry.grid(row=1, column=1, sticky='ew', padx=4, pady=(4, 0))
        self.connect_button = ttk.Button(
            top, text='接続', command=self._start_connect
        )
        self.connect_button.grid(row=1, column=2, pady=(4, 0))
        ttk.Label(top, text='空欄 = SSDP で自動発見').grid(
            row=1, column=3, sticky='w', padx=(4, 0), pady=(4, 0)
        )
        self.status_var = tk.StringVar()
        ttk.Label(top, textvariable=self.status_var).grid(
            row=2, column=0, columnspan=4, sticky='w', pady=(2, 0)
        )
        top.columnconfigure(1, weight=1)

        buttons = ttk.Frame(self, padding=(8, 0, 8, 4))
        buttons.pack(fill='x')
        self.list_button = ttk.Button(
            buttons, text='録画一覧を取得', command=self._start_listing
        )
        self.list_button.pack(side='left')
        self.fetch_button = ttk.Button(
            buttons, text='選択した録画を救出', command=self._start_fetch
        )
        self.fetch_button.pack(side='left', padx=(6, 0))
        self.server_var = tk.StringVar(value='未接続')
        ttk.Label(buttons, textvariable=self.server_var).pack(side='left', padx=(12, 0))

        columns = ('title', 'date', 'channel', 'duration', 'size', 'copies')
        self.tree = ttk.Treeview(self, columns=columns, show='headings', height=10)
        listing_labels: dict[str, str] = {}
        for column, text, width in (
            ('title', 'タイトル', 380),
            ('date', '日時', 150),
            ('channel', 'チャンネル', 150),
            ('duration', '長さ', 70),
            ('size', 'サイズ', 90),
            ('copies', '残コピー', 60),
        ):
            listing_labels[column] = text
            self.tree.column(column, width=width, anchor='w')
        # Initial order: newest recording first.
        self.tree_sorter = SortableTree(
            self.tree, listing_labels, column='date', reverse=True
        )
        self.tree.pack(fill='both', expand=True, padx=8)

        progress_frame = ttk.Frame(self, padding=(8, 4))
        progress_frame.pack(fill='x')
        self.progress = ttk.Progressbar(progress_frame, mode='determinate', maximum=100)
        self.progress.pack(side='left', fill='x', expand=True)
        self.progress_var = tk.StringVar(value='')
        ttk.Label(progress_frame, textvariable=self.progress_var).pack(
            side='left', padx=(8, 0)
        )

        manifest_frame = ttk.LabelFrame(self, text='救出済み (manifest)', padding=4)
        manifest_frame.pack(fill='both', expand=True, padx=8, pady=(2, 4))
        m_columns = ('mtitle', 'mfile', 'mbytes', 'msha', 'mverify')
        self.mtree = ttk.Treeview(
            manifest_frame, columns=m_columns, show='headings', height=6
        )
        manifest_labels: dict[str, str] = {}
        for column, text, width in (
            ('mtitle', 'タイトル', 300),
            ('mfile', 'ファイル', 260),
            ('mbytes', 'サイズ', 90),
            ('msha', 'SHA-256 (先頭16桁)', 130),
            ('mverify', '検証', 60),
        ):
            manifest_labels[column] = text
            self.mtree.column(column, width=width, anchor='w')
        # No initial sort: keep the manifest's own (rescue) order.
        self.mtree_sorter = SortableTree(self.mtree, manifest_labels)
        self.mtree.pack(side='left', fill='both', expand=True)
        ttk.Button(
            manifest_frame, text='ハッシュ検証', command=self._start_verify
        ).pack(side='left', padx=(6, 0), anchor='n')

        log_frame = ttk.LabelFrame(self, text='ログ', padding=4)
        log_frame.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        self.log_text = tk.Text(log_frame, height=8, wrap='word')
        self.log_text.pack(fill='both', expand=True)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _log(self, message: str) -> None:
        self.log_text.insert('end', message + '\n')
        self.log_text.see('end')

    def _display_config(self) -> rescue.Config:
        """The config the entry fields currently describe.

        This is the single source for path resolution: the placement
        status and every action (connect / list / fetch) all derive the
        credentials path from here, so what the status line judges is
        always the file an action would actually use.
        """
        path = self.credentials_var.get().strip()
        host = self.host_var.get().strip() or None
        credentials_changed = bool(path) and Path(path) != self.config.credentials
        if credentials_changed or host != self.config.host:
            return rescue.resolve_config(
                credentials=path if path else None, host=host
            )
        return self.config

    def _current_config(self) -> rescue.Config:
        resolved = self._display_config()
        if resolved is not self.config:
            self.config = resolved
            save_host(self.config, self.config.host)
        # Re-evaluate the placement display on every action: a file the
        # user placed after launch (same path, so nothing "changed")
        # must show up without restarting the GUI.
        self._refresh_credentials_status()
        return self.config

    def _refresh_credentials_status(self) -> None:
        self.status_var.set(
            f"配置状態: {credentials_status(self._display_config())}"
        )

    def _choose_credentials(self) -> None:
        chosen = filedialog.askopenfilename(
            title='credentials.env を選択',
            initialdir=str(self.config.credentials.parent),
            filetypes=[('env file', 'credentials.env'), ('all files', '*.*')],
        )
        if chosen:
            self.credentials_var.set(chosen)
            self.config = rescue.resolve_config(
                credentials=chosen, host=self.host_var.get().strip() or None
            )
            self._refresh_credentials_status()
            self._log(f'資格情報ファイルのパスを変更: {chosen}')

    def _open_credentials_folder(self) -> None:
        folder = self._current_config().credentials.parent
        try:
            if sys.platform == 'win32':
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', str(folder)])
            else:
                subprocess.Popen(['xdg-open', str(folder)])
        except Exception as error:  # opening a folder is a nicety, not core
            self._log(f'フォルダを開けませんでした: {error}')

    # ------------------------------------------------------------------
    # connection check
    # ------------------------------------------------------------------
    def _start_connect(self) -> None:
        if self.fetching or self.connecting:
            return
        config = self._current_config()
        self.connecting = True
        self.connect_button.config(state='disabled')
        self.list_button.config(state='disabled')
        self.fetch_button.config(state='disabled')
        if config.host:
            self.server_var.set(f'接続中… ({config.host})')
            self._log(f'接続を確認しています… ({config.host} へ直接接続)')
        else:
            self.server_var.set('接続中… (SSDP で発見中)')
            self._log('接続を確認しています… (SSDP で自動発見)')

        def worker() -> None:
            try:
                server = rescue.probe_server(config)
                self.events.put(('connected', server))
            except SystemExit as error:
                self.events.put(('connect_error', str(error)))
            except Exception as error:  # surface anything unexpected
                self.events.put(('connect_error', repr(error)))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_connect(self) -> None:
        self.connecting = False
        self.connect_button.config(state='normal')
        self.list_button.config(state='normal')
        self.fetch_button.config(state='normal')

    # ------------------------------------------------------------------
    # listing
    # ------------------------------------------------------------------
    def _start_listing(self) -> None:
        if self.fetching or self.connecting:
            return
        self.list_button.config(state='disabled')
        self._log('録画一覧を取得しています…')
        config = self._current_config()

        def worker() -> None:
            try:
                listing = rescue.run_listing(config)
                self.events.put(('listing', listing))
            except SystemExit as error:
                self.events.put(('error', f'一覧取得に失敗しました: {error}'))
            except Exception as error:  # surface anything unexpected
                self.events.put(('error', f'一覧取得に失敗しました: {error!r}'))

        threading.Thread(target=worker, daemon=True).start()

    def _show_listing(self, listing: dict) -> None:
        self.listing = listing
        server = listing.get('server', {})
        self.server_var.set(
            f"接続済み: {server.get('friendly_name', '?')} "
            f"({server.get('location', '')})"
        )
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.tree_sorter.clear()
        self.recordings.clear()
        for recording in listing.get('recordings', []):
            content_id = recording.get('content_id') or recording['id']
            self.recordings[content_id] = recording
            copies = recording.get('remaining_copy_count', {})
            copies_text = (
                str(copies.get('value')) if copies.get('status') == 'known' else '?'
            )
            # Scheduled start time when the listing has it, else the recording start.
            started = recording.get('scheduled_start_time') or recording.get('date') or ''
            date = started.replace('T', ' ')[:16]
            self.tree.insert(
                '',
                'end',
                iid=content_id,
                values=(
                    recording.get('title', ''),
                    date,
                    recording.get('channel_name', ''),
                    recording.get('duration', ''),
                    format_bytes(original_size(recording)),
                    copies_text,
                ),
            )
            self.tree_sorter.set_keys(
                content_id,
                {
                    'title': text_key(recording.get('title')),
                    'date': date or None,
                    'channel': text_key(recording.get('channel_name')),
                    'duration': duration_key(recording.get('duration')),
                    'size': number_key(original_size(recording)),
                    'copies': (
                        number_key(copies.get('value'))
                        if copies.get('status') == 'known'
                        else None
                    ),
                },
            )
        self.tree_sorter.apply()
        self._log(f"録画 {len(self.recordings)} 件を取得しました")

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------
    def _start_fetch(self) -> None:
        if self.fetching or self.connecting:
            return
        if self.listing is None:
            messagebox.showinfo('録画救出', '先に「録画一覧を取得」を実行してください')
            return
        selected = [self.recordings[iid] for iid in self.tree.selection()]
        if not selected:
            messagebox.showinfo('録画救出', '救出する録画を選択してください')
            return
        config = self._current_config()
        listing = self.listing
        self.fetching = True
        self.fetch_button.config(state='disabled')
        self.list_button.config(state='disabled')

        def emit(message: str) -> None:
            self.events.put(('log', message))

        def worker() -> None:
            succeeded = 0
            for recording in selected:
                filename = rescue.recording_filename(recording)
                self.events.put(
                    (
                        'fetch_begin',
                        (
                            config.out_dir / f'.{filename}.partial',
                            original_size(recording),
                            recording.get('title', ''),
                        ),
                    )
                )
                try:
                    ok = rescue.fetch_one(
                        config, listing, recording, force=False, timeout=0, emit=emit
                    )
                except Exception as error:
                    emit(f'FAILED: {recording.get("title")} ({error!r})')
                    ok = False
                self.events.put(('fetch_end', None))
                succeeded += 1 if ok else 0
            self.events.put(('fetch_all_done', (succeeded, len(selected))))

        threading.Thread(target=worker, daemon=True).start()

    def _rate_text(self, now: float, size: int) -> str:
        """Transfer rate suffix for the progress line.

        The main figure is a moving average over the last few seconds
        of .partial growth (instantaneous 300 ms deltas jitter too much
        to read); the overall average since the fetch began follows in
        parentheses. Rates are decimal MB/s, matching the figures in
        the tool README (about 11 MB/s on a wired LAN).
        """
        window_start = now - 5.0
        base = next(
            (sample for sample in self.progress_samples if sample[0] >= window_start),
            self.progress_samples[0] if self.progress_samples else None,
        )
        recent = None
        if base is not None and now - base[0] >= 1.0 and size > base[1]:
            recent = (size - base[1]) / (now - base[0]) / 1_000_000
        overall = None
        if (
            self.fetch_started_at is not None
            and now - self.fetch_started_at >= 1.0
            and size > 0
        ):
            overall = size / (now - self.fetch_started_at) / 1_000_000
        if recent is not None:
            text = f' — {recent:.1f} MB/s'
            if overall is not None and f'{overall:.1f}' != f'{recent:.1f}':
                text += f' (平均 {overall:.1f} MB/s)'
            return text
        if overall is not None:
            return f' — {overall:.1f} MB/s'
        return ''

    def _poll_progress(self) -> None:
        if self.active_partial is not None and self.active_expected:
            try:
                size = self.active_partial.stat().st_size
            except OSError:
                size = 0
            now = time.monotonic()
            self.progress_samples.append((now, size))
            cutoff = now - 10.0
            while len(self.progress_samples) > 2 and self.progress_samples[0][0] < cutoff:
                self.progress_samples.pop(0)
            percent = min(100.0, 100.0 * size / self.active_expected)
            self.progress['value'] = percent
            self.progress_var.set(
                f'{format_bytes(size)} / {format_bytes(self.active_expected)}'
                f' ({percent:.0f}%){self._rate_text(now, size)}'
            )
        self.after(300, self._poll_progress)

    # ------------------------------------------------------------------
    # manifest / verify
    # ------------------------------------------------------------------
    def _refresh_manifest(self, verify_results: dict[str, str] | None = None) -> None:
        for item in self.mtree.get_children():
            self.mtree.delete(item)
        self.mtree_sorter.clear()
        try:
            entries = rescue.read_manifest(self.config)
        except Exception as error:
            self._log(f'manifest を読めませんでした: {error!r}')
            return
        for entry in entries:
            result = (verify_results or {}).get(entry['id'], '')
            self.mtree.insert(
                '',
                'end',
                iid=entry['id'],
                values=(
                    entry.get('title', ''),
                    entry.get('file', ''),
                    format_bytes(entry.get('bytes')),
                    entry.get('sha256', '')[:16],
                    result,
                ),
            )
            self.mtree_sorter.set_keys(
                entry['id'],
                {
                    'mtitle': text_key(entry.get('title')),
                    'mfile': text_key(entry.get('file')),
                    'mbytes': number_key(entry.get('bytes')),
                    'msha': text_key(entry.get('sha256')),
                    'mverify': text_key(result),
                },
            )
        self.mtree_sorter.apply()

    def _start_verify(self) -> None:
        config = self.config

        def worker() -> None:
            results: dict[str, str] = {}
            for entry in rescue.read_manifest(config):
                path = config.out_dir / entry['file']
                if not path.exists():
                    results[entry['id']] = 'ファイルなし'
                else:
                    results[entry['id']] = (
                        'OK' if rescue.sha256_of(path) == entry['sha256'] else '不一致'
                    )
            self.events.put(('verified', results))

        threading.Thread(target=worker, daemon=True).start()
        self._log('manifest のハッシュを検証しています…')

    # ------------------------------------------------------------------
    # event loop glue
    # ------------------------------------------------------------------
    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == 'log':
                    self._log(payload)
                elif kind == 'connected':
                    self._finish_connect()
                    self.server_var.set(
                        f"接続済み: {payload.get('friendly_name', '?')} "
                        f"({payload.get('location', '')})"
                    )
                    self._log(
                        f"接続できました: {payload.get('friendly_name', '?')} "
                        f"({payload.get('location', '')})"
                    )
                elif kind == 'connect_error':
                    self._finish_connect()
                    self.server_var.set(f'未接続: {payload}')
                    self._log(f'接続できませんでした: {payload}')
                elif kind == 'listing':
                    self._show_listing(payload)
                    self.list_button.config(state='normal')
                elif kind == 'error':
                    self._log(payload)
                    self.list_button.config(state='normal')
                    messagebox.showerror('録画救出', payload)
                elif kind == 'fetch_begin':
                    partial, expected, title = payload
                    self.active_partial = partial
                    self.active_expected = expected
                    self.fetch_started_at = time.monotonic()
                    self.progress_samples = []
                    self.progress['value'] = 0
                    self.progress_var.set(f'救出中: {title}')
                elif kind == 'fetch_end':
                    self.active_partial = None
                    self.active_expected = None
                    self.fetch_started_at = None
                    self.progress_samples = []
                elif kind == 'fetch_all_done':
                    succeeded, total = payload
                    self.fetching = False
                    self.fetch_button.config(state='normal')
                    self.list_button.config(state='normal')
                    self.progress['value'] = 100 if succeeded == total else 0
                    self.progress_var.set('')
                    self._log(f'救出完了: {succeeded}/{total} 件成功')
                    self._refresh_credentials_status()
                    self._refresh_manifest()
                elif kind == 'verified':
                    self._refresh_manifest(payload)
                    self._log('ハッシュ検証が終わりました')
        except queue.Empty:
            pass
        self.after(120, self._drain_events)


def main() -> int:
    app = RescueApp()
    app.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
