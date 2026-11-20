#!/usr/bin/env python3
"""DTCP-IP rescue tool driver (list / fetch / fetch-all).

Fetches recordings from a DTCP-IP recorder (nasne) over the regular
DTCP-IP path (AKE + stream reception) using pydtcpip as the engine.

Design rule (agreed with the owner): this program never reads, stores,
prints, or passes through credential values. pydtcpip itself loads the
credentials from the credentials.env file -- the only file the owner
writes. PYDTCPIP_* environment variables are removed from the child
environment so that file is the sole source, and the credential file is
only ever referred to by path.

Transfers use pydtcpip's `stream` command (playback-style reception,
which does not consume copies and does not change the source). The
`move` / `receive` dubbing commands are deliberately never used.

Stdlib only; run with Python 3.10+.

Saved file names: <content id>_<yyyy-MM-dd>「<title>」.ts, where the date is
the broadcast day (the day rolls over at 05:00) taken from the recording's
scheduled start time, or its recording start time when that is absent.
The saved file's modification time is set to that same start time.

Configuration (first match wins):
  credentials : --credentials / $RESCUE_CREDENTIALS / <base>/data/credentials.env
  data dir    : --data-dir    / $RESCUE_DATA_DIR    / directory of the credentials file
  output dir  : --out-dir     / $RESCUE_OUT_DIR     / <base>/out
  logs dir    : --logs-dir    / $RESCUE_LOGS_DIR    / <base>/logs
  nasne host  : --host        / $RESCUE_HOST        / SSDP discovery
                (manual registration: fetch the device description from
                the given address directly, skipping multicast discovery,
                so routed paths such as VPN / subnet routing also work)
  pydtcpip    : --pydtcpip-home / $PYDTCPIP_HOME    / a pydtcpip checkout
                containing .venv (bin/ on POSIX, Scripts/ on Windows);
                falls back to a pydtcpip importable by this interpreter
                and a `pydtcpip` command on PATH.
<base> is the legacy work-area layout when it exists on this machine
(compatibility default), otherwise this tool's own directory.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
LIST_SCRIPT = TOOL_DIR / 'list_recordings.py'

# Legacy layouts from the original work area (compatibility defaults,
# used only when they actually exist on this machine).
LEGACY_ROOT = (
    Path.home() / 'GitTmp/myClaude/SonyNasne-DTCP.work/2026-11-17-dtcpip-rescue'
)
LEGACY_PYDTCPIP_HOME = (
    Path.home() / 'GitTmp/myClaude/SonyNasne-DTCP.work/2026-11-15-pydtcpip/pydtcpip'
)

# A recording's calendar date is its broadcast day, which rolls over at
# 05:00 (a 00:35 late-night show belongs to the previous day).
BROADCAST_DAY_START_HOUR = 5
# Byte budget for the title part of a file name. The id / date prefix, the
# surrounding 「」 and the extension add about 40 bytes, so even the longest
# derived name (.partial) stays under 255 bytes.
TITLE_MAX_BYTES = 180


@dataclass
class Config:
    credentials: Path
    data_dir: Path
    out_dir: Path
    logs_dir: Path
    pydtcpip_home: Path | None
    host: str | None = None

    @property
    def listings_dir(self) -> Path:
        return self.out_dir / 'listings'

    @property
    def manifest_path(self) -> Path:
        return self.out_dir / 'manifest.json'

    @property
    def sha256sums_path(self) -> Path:
        return self.out_dir / 'SHA256SUMS.txt'


def _path_from(value: str | None) -> Path | None:
    return Path(value).expanduser() if value else None


def resolve_config(
    *,
    credentials: str | None = None,
    data_dir: str | None = None,
    out_dir: str | None = None,
    logs_dir: str | None = None,
    pydtcpip_home: str | None = None,
    host: str | None = None,
) -> Config:
    """Resolve all paths from explicit values, environment, and defaults."""
    base = LEGACY_ROOT if LEGACY_ROOT.is_dir() else TOOL_DIR

    credentials_path = (
        _path_from(credentials)
        or _path_from(os.environ.get('RESCUE_CREDENTIALS'))
        or base / 'data' / 'credentials.env'
    )
    data_path = (
        _path_from(data_dir)
        or _path_from(os.environ.get('RESCUE_DATA_DIR'))
        or credentials_path.parent
    )
    out_path = (
        _path_from(out_dir) or _path_from(os.environ.get('RESCUE_OUT_DIR')) or base / 'out'
    )
    logs_path = (
        _path_from(logs_dir)
        or _path_from(os.environ.get('RESCUE_LOGS_DIR'))
        or base / 'logs'
    )

    home = (
        _path_from(pydtcpip_home)
        or _path_from(os.environ.get('PYDTCPIP_HOME'))
        or (LEGACY_PYDTCPIP_HOME if LEGACY_PYDTCPIP_HOME.is_dir() else None)
    )
    host_value = host or os.environ.get('RESCUE_HOST') or None
    return Config(
        credentials=credentials_path,
        data_dir=data_path,
        out_dir=out_path,
        logs_dir=logs_path,
        pydtcpip_home=home,
        host=host_value,
    )


def _venv_dirs(home: Path) -> list[Path]:
    venv = home / '.venv'
    return [venv / 'bin', venv / 'Scripts']


def pydtcpip_python(config: Config) -> Path | None:
    """Python interpreter that can import pydtcpip (for list_recordings.py)."""
    override = _path_from(os.environ.get('PYDTCPIP_PYTHON'))
    if override is not None:
        return override
    if config.pydtcpip_home is not None:
        for directory in _venv_dirs(config.pydtcpip_home):
            for name in ('python', 'python.exe'):
                candidate = directory / name
                if candidate.exists():
                    return candidate
    # Fall back to this interpreter when pydtcpip is importable by it.
    probe = subprocess.run(
        [sys.executable, '-c', 'import pydtcpip'],
        capture_output=True,
    )
    if probe.returncode == 0:
        return Path(sys.executable)
    return None


def pydtcpip_cli(config: Config) -> Path | None:
    """The `pydtcpip` command used for `stream` transfers."""
    override = _path_from(os.environ.get('PYDTCPIP_BIN'))
    if override is not None:
        return override
    if config.pydtcpip_home is not None:
        for directory in _venv_dirs(config.pydtcpip_home):
            for name in ('pydtcpip', 'pydtcpip.exe'):
                candidate = directory / name
                if candidate.exists():
                    return candidate
    found = shutil.which('pydtcpip')
    return Path(found) if found else None


def stamp() -> str:
    return datetime.now().strftime('%Y%m%d-%H%M%S')


def child_env() -> dict[str, str]:
    """Child environment: PYDTCPIP_* removed (the credentials file is the
    only source), and UTF-8 forced so Japanese titles survive on Windows
    consoles / pipes whose default encoding is not UTF-8."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith('PYDTCPIP_')
    }
    env['PYTHONUTF8'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    return env


def sanitize_title(title: str) -> str:
    """Make a title safe for a filename.

    Same character rules as pydtcpip's CLI; the length limit is lower
    (TITLE_MAX_BYTES) to leave room for the id / date prefix.
    """
    title = title.translate(str.maketrans(dict.fromkeys('/\\:*?"<>|\r\n\t', '_')))
    title = title.encode('utf-8')[:TITLE_MAX_BYTES].decode('utf-8', errors='ignore').rstrip(' .')
    return title or 'recording'


def sanitize_id(object_id: str) -> str:
    return ''.join(char if char.isalnum() or char in '-_' else '_' for char in object_id)


def recording_start(recording: dict) -> datetime | None:
    """Start time of a recording: `scheduled_start_time`, else `date`.

    `date` is the recording start, which the recorder sets a few seconds
    before the scheduled start. Returns None when neither value can be read.
    """
    for key in ('scheduled_start_time', 'date'):
        value = recording.get(key)
        if not value:
            continue
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            continue
    return None


def broadcast_date(recording: dict) -> str:
    """Broadcast day (yyyy-MM-dd) of a recording; the day rolls over at 05:00.

    The start time is read with its own UTC offset -- no conversion to this
    machine's time zone. Returns 'unknown' when there is no readable start.
    """
    started = recording_start(recording)
    if started is None:
        return 'unknown'
    return (started - timedelta(hours=BROADCAST_DAY_START_HOUR)).date().isoformat()


def recording_basename(recording: dict) -> str:
    """File name without extension: <id>_<yyyy-MM-dd>「<title>」"""
    content_id = recording.get('content_id') or recording['id']
    return (
        f"{sanitize_id(content_id)}_{broadcast_date(recording)}"
        f"「{sanitize_title(recording['title'])}」"
    )


def recording_filename(recording: dict) -> str:
    return f'{recording_basename(recording)}.ts'


def _pydtcpip_python_or_exit(config: Config) -> Path:
    python = pydtcpip_python(config)
    if python is None:
        raise SystemExit(
            'no Python interpreter with pydtcpip found. '
            'Set PYDTCPIP_HOME to a pydtcpip checkout (with .venv) '
            'or install pydtcpip for this interpreter.'
        )
    return python


def _list_command(config: Config, python: Path, probe: bool = False) -> list[str]:
    command = [str(python), str(LIST_SCRIPT)]
    if probe:
        command.append('--probe')
    if config.host:
        command += ['--host', config.host]
    return command


def run_listing(config: Config) -> dict:
    """Run the enumeration script and save the listing under out/listings/."""
    python = _pydtcpip_python_or_exit(config)
    config.listings_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        _list_command(config, python),
        capture_output=True,
        encoding='utf-8',
        errors='replace',
        env=child_env(),
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f'listing failed (exit {proc.returncode})')
    listing = json.loads(proc.stdout)
    path = config.listings_dir / f'{stamp()}.json'
    path.write_text(proc.stdout, encoding='utf-8')
    return listing


def probe_server(config: Config) -> dict:
    """Connection check only: locate the recorder (manual address or SSDP
    discovery) and fetch its device description, without browsing.

    Returns the server info dict. Raises SystemExit carrying the reason
    (the enumeration script's own message) when the recorder cannot be
    reached, so callers can show *why* the connection failed.
    """
    python = _pydtcpip_python_or_exit(config)
    proc = subprocess.run(
        _list_command(config, python, probe=True),
        capture_output=True,
        encoding='utf-8',
        errors='replace',
        env=child_env(),
    )
    if proc.returncode != 0:
        lines = [line for line in proc.stderr.strip().splitlines() if line.strip()]
        raise SystemExit(lines[-1] if lines else f'probe failed (exit {proc.returncode})')
    return json.loads(proc.stdout)['server']


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(config: Config) -> list[dict]:
    if config.manifest_path.exists():
        return json.loads(config.manifest_path.read_text(encoding='utf-8'))['entries']
    return []


def update_manifest(config: Config, entry: dict) -> None:
    entries = [e for e in read_manifest(config) if e['id'] != entry['id']]
    # (manifest entries are keyed by content_id)
    entries.append(entry)
    entries.sort(key=lambda item: item['id'])
    config.manifest_path.write_text(
        json.dumps(
            {'generated_at': datetime.now().isoformat(timespec='seconds'), 'entries': entries},
            ensure_ascii=False,
            indent=2,
        )
        + '\n',
        encoding='utf-8',
    )
    lines = [f"{item['sha256']}  {item['file']}" for item in entries]
    config.sha256sums_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def fetch_one(
    config: Config,
    listing: dict,
    recording: dict,
    *,
    force: bool,
    timeout: float,
    emit=print,
) -> bool:
    """Fetch one recording via pydtcpip `stream`. Returns True on success."""
    cli = pydtcpip_cli(config)
    if cli is None:
        emit(
            'pydtcpip command not found. Set PYDTCPIP_HOME to a pydtcpip '
            'checkout (with .venv) or put pydtcpip on PATH.'
        )
        return False
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.out_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    object_id = recording['id']
    content_id = recording.get('content_id') or object_id
    filename = recording_filename(recording)
    final = config.out_dir / filename
    partial = config.out_dir / f'.{filename}.partial'
    if final.exists() and not force:
        emit(f'skip (already fetched): {filename}')
        return True

    log_path = config.logs_dir / f'{stamp()}-fetch-{sanitize_id(object_id)}.log'
    command = [
        str(cli),
        '--data-dir',
        str(config.data_dir),
        'stream',
        '--server',
        listing['server']['location'],
        '--id',
        object_id,
        '--credentials',
        str(config.credentials),
    ]
    with log_path.open('w', encoding='utf-8') as log:
        log.write(f'# fetch started {datetime.now().isoformat(timespec="seconds")}\n')
        log.write(f'# recording id: {object_id}\n')
        log.write(f'# title: {recording["title"]}\n')
        log.write(f'# credentials file (path only): {config.credentials}\n')
        log.write(f'# credentials file present: {config.credentials.exists()}\n')
        log.flush()
        with partial.open('wb') as out:
            proc = subprocess.run(
                command,
                stdout=out,
                stderr=log,
                env=child_env(),
                timeout=timeout if timeout > 0 else None,
            )
        size = partial.stat().st_size
        log.write(f'# fetch ended {datetime.now().isoformat(timespec="seconds")}\n')
        log.write(f'# pydtcpip exit code: {proc.returncode}\n')
        log.write(f'# bytes written: {size}\n')

    if proc.returncode != 0 or size == 0:
        emit(f'FAILED: {recording["title"]} (exit {proc.returncode}, {size} bytes)')
        emit(f'  log: {log_path}')
        emit(f'  partial data kept at: {partial}')
        return False

    os.replace(partial, final)
    started = recording_start(recording)
    if started is not None:
        # Modification time = the recording's start time (not the download time).
        try:
            os.utime(final, (final.stat().st_atime, started.timestamp()))
        except (OSError, OverflowError, ValueError) as error:
            emit(f'note: could not set the modification time of {filename}: {error}')
    digest = sha256_of(final)
    fetched_at = datetime.now().isoformat(timespec='seconds')
    sidecar = dict(recording)
    sidecar['fetched'] = {
        'at': fetched_at,
        'file': filename,
        'bytes': size,
        'sha256': digest,
        'tool': 'pydtcpip stream (DTCP-IP, no copy consumed)',
        'server_location': listing['server']['location'],
    }
    (config.out_dir / f'{filename}.json').write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
    )
    update_manifest(
        config,
        {
            'id': content_id,
            'object_id': object_id,
            'title': recording['title'],
            'file': filename,
            'bytes': size,
            'sha256': digest,
            'fetched_at': fetched_at,
        },
    )
    emit(f'OK: {filename} ({size} bytes, sha256 {digest})')
    return True


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--credentials', help='Path to credentials.env')
    parser.add_argument('--data-dir', help='Directory for pydtcpip data (srm.bin)')
    parser.add_argument('--out-dir', help='Output directory for rescued files')
    parser.add_argument('--logs-dir', help='Directory for run logs')
    parser.add_argument('--pydtcpip-home', help='pydtcpip checkout containing .venv')
    parser.add_argument(
        '--host',
        help='Recorder address for manual registration (skips SSDP discovery): '
        'a host name or IP, host:port, or a full device description URL',
    )


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
        # Keep Japanese titles printable on non-UTF-8 consoles (Windows).
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    parser = argparse.ArgumentParser(description='DTCP-IP rescue tool driver.')
    sub = parser.add_subparsers(dest='command', required=True)

    list_parser = sub.add_parser('list', help='List current recordings as JSON (no credentials).')
    add_common_options(list_parser)

    fetch_parser = sub.add_parser('fetch', help='Fetch one recording by ID.')
    fetch_parser.add_argument('--id', required=True, help='Recording ID from `list`')
    fetch_parser.add_argument('--force', action='store_true', help='Refetch even if already saved')
    fetch_parser.add_argument(
        '--timeout', type=float, default=0, help='Per-transfer timeout in seconds (0 = none)'
    )
    add_common_options(fetch_parser)

    all_parser = sub.add_parser('fetch-all', help='Fetch every recording in the listing.')
    all_parser.add_argument('--force', action='store_true', help='Refetch even if already saved')
    all_parser.add_argument(
        '--timeout', type=float, default=0, help='Per-transfer timeout in seconds (0 = none)'
    )
    add_common_options(all_parser)

    args = parser.parse_args()
    config = resolve_config(
        credentials=args.credentials,
        data_dir=args.data_dir,
        out_dir=args.out_dir,
        logs_dir=args.logs_dir,
        pydtcpip_home=args.pydtcpip_home,
        host=args.host,
    )

    if args.command == 'list':
        listing = run_listing(config)
        print(json.dumps(listing, ensure_ascii=False, indent=2))
        return 0

    listing = run_listing(config)
    if args.command == 'fetch':
        recording = next(
            (
                r
                for r in listing['recordings']
                if r['id'] == args.id or r.get('content_id') == args.id
            ),
            None,
        )
        if recording is None:
            print(f'recording ID not found in the current listing: {args.id}', file=sys.stderr)
            return 2
        return 0 if fetch_one(config, recording=recording, listing=listing, force=args.force, timeout=args.timeout) else 1

    # fetch-all
    results = [
        fetch_one(config, listing, recording, force=args.force, timeout=args.timeout)
        for recording in listing['recordings']
    ]
    print(f'fetch-all: {sum(results)}/{len(results)} succeeded')
    return 0 if all(results) else 1


if __name__ == '__main__':
    sys.exit(main())
