#!/usr/bin/env python3
"""Enumerate recordings on a DTCP-IP recorder (nasne) without credentials.

Uses pydtcpip's DLNA stack (SSDP discovery + ContentDirectory Browse) and
prints a JSON listing: title, ID, resources (res URL / protocolInfo / size),
and protection info (remaining copy count from av:COGCount, Move support).

With `--host`, discovery is skipped (manual registration): the device
description is fetched directly from the given recorder address and only
the Browse step runs over unicast HTTP. This works across routed paths
(VPN, subnet routing) where SSDP multicast does not reach.

With `--probe`, only the connection check runs: the recorder is located
(manual address or SSDP discovery) and its server info is printed as
JSON, without browsing any recordings.

Run with pydtcpip's venv Python so that `pydtcpip` is importable:
    <pydtcpip>/.venv/bin/python list_recordings.py [--host 192.168.11.129]
"""

import argparse
import asyncio
import json
import os
import sys

from pydtcpip.dlna.client import DLNAClient
from pydtcpip.models import Container, Recording


def content_id_of(object_id: str) -> str:
    """Extract the stable content ID (`pvr?id=` value) from a DIDL object ID.

    The same recording appears under several containers (all / genre / date)
    with different object IDs; the `pvr?id=` part identifies the recording.
    """
    marker = 'pvr?id='
    if marker in object_id:
        return object_id.split(marker, 1)[1]
    return object_id


async def collect_recordings(server) -> list[Recording]:
    """Recursively browse from the root container and collect recordings."""
    seen_containers: set[str] = set()
    recordings: dict[str, Recording] = {}

    async def walk(object_id: str, depth: int) -> None:
        if object_id in seen_containers or depth > 8:
            return
        seen_containers.add(object_id)
        for item in await server.browse_all(object_id):
            if isinstance(item, Recording):
                # Live tuner channels are listed as videoBroadcast items;
                # they are not recordings (same distinction as pydtcpip's CLI).
                if item.upnp_class.startswith('object.item.videoItem.videoBroadcast'):
                    continue
                key = content_id_of(item.id)
                existing = recordings.get(key)
                # Prefer the canonical flat-list entry (0/video/all/...)
                if existing is None or (
                    item.id.startswith('0/video/all/') and not existing.id.startswith('0/video/all/')
                ):
                    recordings[key] = item
            elif isinstance(item, Container):
                await walk(item.id, depth + 1)

    await walk('0', 0)
    return list(recordings.values())


def protection_dump(value) -> dict:
    return {'status': value.status, 'value': value.value}


def iso_or_none(value) -> str | None:
    """JSON-safe ISO 8601 text for a timestamp that may be a str or datetime."""
    if value is None:
        return None
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


def recording_dump(recording: Recording) -> dict:
    original = recording.original_resource
    resources = []
    for resource in recording.resources:
        resources.append(
            {
                'url': resource.url,
                'protocol_info': resource.protocol_info.raw,
                'size': resource.size,
                'cleartext_size': resource.cleartext_size,
                'is_original': resource is original,
            }
        )
    return {
        'id': recording.id,
        'content_id': content_id_of(recording.id),
        'parent_id': recording.parent_id,
        'title': recording.title,
        'date': recording.date,
        # Used for the saved file name's broadcast day (rescue.py falls back
        # to `date` when this is absent).
        'scheduled_start_time': iso_or_none(getattr(recording, 'scheduled_start_time', None)),
        'duration': recording.duration,
        'channel_name': recording.channel_name,
        'description': recording.description,
        'long_description': recording.long_description,
        'genres': list(recording.genres),
        'upnp_class': recording.upnp_class,
        'remaining_copy_count': protection_dump(recording.protection.remaining_copy_count),
        'is_move_supported': protection_dump(recording.protection.is_move_supported),
        'is_transaction_move_supported': protection_dump(
            recording.protection.is_transaction_move_supported
        ),
        'resources': resources,
    }


def location_for_host(host: str) -> str:
    """Device description URL for a manually specified recorder.

    Accepts a bare host (`192.168.11.129`, which gets the nasne DMS port
    58888), a `host:port` pair, or a full description URL.
    """
    if host.startswith(('http://', 'https://')):
        return host
    if ':' in host:
        return f'http://{host}/'
    return f'http://{host}:58888/'


def server_dump(server) -> dict:
    return {
        'friendly_name': server.description.friendly_name,
        'manufacturer': server.description.manufacturer,
        'model_name': server.description.model_name,
        'profile': server.profile.name,
        'location': server.description.location,
    }


async def run(timeout: float, host: str | None, probe: bool = False) -> int:
    async with DLNAClient() as dlna:
        if host:
            # Manual registration: fetch the description directly instead
            # of SSDP discovery (multicast does not cross routed paths).
            try:
                server = await dlna.get_server(location_for_host(host))
            except Exception as error:
                print(f'could not reach the recorder at {host}: {error}', file=sys.stderr)
                return 1
        else:
            servers = await dlna.discover(timeout=timeout, port=57000)
            if not servers:
                print('no media servers found', file=sys.stderr)
                return 1
            server = next((s for s in servers if s.profile.name == 'sie_nasne'), None)
            if server is None:
                if len(servers) != 1:
                    print(
                        'multiple servers found, none matched the sie_nasne profile',
                        file=sys.stderr,
                    )
                    return 1
                server = servers[0]
        if probe:
            # Connection check only: report the server, skip the Browse.
            json.dump({'server': server_dump(server)}, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write('\n')
            return 0
        recordings = await collect_recordings(server)
        result = {
            'server': server_dump(server),
            'recordings': [recording_dump(recording) for recording in recordings],
        }
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write('\n')
    return 0


def main() -> int:
    # The listing JSON carries Japanese titles; force UTF-8 on stdout so
    # non-UTF-8 consoles/pipes (e.g. Windows cp932) cannot corrupt it.
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    parser = argparse.ArgumentParser(description='List recordings on the nasne (no credentials).')
    parser.add_argument('--timeout', type=float, default=5.0, help='SSDP discovery timeout in seconds')
    parser.add_argument(
        '--probe',
        action='store_true',
        help='Connection check only: locate the recorder and print its '
        'server info as JSON, without browsing recordings',
    )
    parser.add_argument(
        '--host',
        default=os.environ.get('RESCUE_HOST'),
        help='Recorder address for manual registration (skips SSDP discovery): '
        'a host name or IP (port 58888 is assumed), host:port, or a full '
        'device description URL. Defaults to $RESCUE_HOST.',
    )
    args = parser.parse_args()
    return asyncio.run(run(args.timeout, args.host, probe=args.probe))


if __name__ == '__main__':
    sys.exit(main())
