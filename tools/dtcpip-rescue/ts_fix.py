#!/usr/bin/env python3
"""Convert a rescued nasne TS (ARIB partial TS) into a regular broadcast-style TS.

The TS that rescue.py saves (pydtcpip `stream` output) is an ARIB
"partial TS" (ARIB STD-B21 / TR-B14), as the recorder stores it:
  * 192-byte packets (4-byte TTS timestamp + 188-byte TS packet)
  * no NIT / SDT / EIT / TOT; programme information lives in the SIT (PID 0x1F)

Tools that read the channel name, programme title, genre or broadcast time
from SDT/EIT/TOT therefore find nothing. This converter:
  1. strips the 4-byte TTS header (192 -> 188 bytes)
  2. rewrites the PAT network entry (program 0) from SIT 0x1F to NIT 0x10
  3. synthesises from the SIT:
       NIT actual (PID 0x10, table 0x40)
       SDT actual (PID 0x11, table 0x42)
       EIT p/f actual (PID 0x12, table 0x4E) -- updated when the SIT's event changes
       TOT (PID 0x14, table 0x73) -- JST from the SIT's partialTS_time descriptor,
                                     advanced by PCR
  All descriptors (ARIB 8-unit strings included) are copied byte-for-byte.
  SIT/DIT packets are kept as-is. Audio/video packets are untouched.

The input is never modified. Output is written as "<name>.partial" and
renamed when complete. A 188-byte input (e.g. an already converted file) is
accepted too: the previously generated tables are dropped and rebuilt, so
running the converter again yields the same bytes.

Note: the SHA-256 values in rescue.py's manifest / sidecar JSON describe
the rescued (unconverted) file, not the converted one.

Stdlib only; run with Python 3.10+. Usage:
  python ts_fix.py INPUT.ts [INPUT2.ts ...] [-o OUTDIR] [--overwrite]
Default OUTDIR is "<input folder>/fixed".
"""

import argparse
import sys
from pathlib import Path

# ---------------------------------------------------------------- CRC / packets

_CRC_TABLE = []
for _i in range(256):
    _c = _i << 24
    for _ in range(8):
        _c = ((_c << 1) ^ 0x04C11DB7) if _c & 0x80000000 else (_c << 1)
    _CRC_TABLE.append(_c & 0xFFFFFFFF)


def crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((crc >> 24) ^ b) & 0xFF]
    return crc


def with_crc(body: bytes) -> bytes:
    return body + crc32_mpeg(body).to_bytes(4, 'big')


def long_section(table_id: int, ext: int, version: int, sec_no: int, last_sec_no: int,
                 payload: bytes) -> bytes:
    """Section with section_syntax_indicator=1 (adds CRC)."""
    length = 5 + len(payload) + 4
    head = bytes([table_id, 0xF0 | (length >> 8), length & 0xFF,
                  ext >> 8, ext & 0xFF, 0xC1 | ((version & 0x1F) << 1), sec_no, last_sec_no])
    return with_crc(head + payload)


def loop_len(n: int, top: int = 0xF0) -> bytes:
    return bytes([top | (n >> 8), n & 0xFF])


class Packetizer:
    def __init__(self):
        self.cc = {}

    def packets(self, pid: int, section: bytes) -> list[bytes]:
        data = b'\x00' + section  # pointer_field = 0
        out = []
        first = True
        while data:
            chunk, data = data[:184], data[184:]
            cc = self.cc.get(pid, 0)
            self.cc[pid] = (cc + 1) & 0xF
            hdr = bytes([0x47, (0x40 if first else 0) | (pid >> 8), pid & 0xFF, 0x10 | cc])
            out.append(hdr + chunk + b'\xFF' * (184 - len(chunk)))
            first = False
        return out


# ---------------------------------------------------------------- time helpers

def bcd(v: int) -> int:
    return ((v // 10) << 4) | (v % 10)


def unbcd(b: int) -> int:
    return (b >> 4) * 10 + (b & 0xF)


def jst_bytes_to_seconds(b: bytes) -> int:
    """MJD(16) + BCD hh mm ss -> seconds since MJD epoch."""
    mjd = int.from_bytes(b[0:2], 'big')
    return mjd * 86400 + unbcd(b[2]) * 3600 + unbcd(b[3]) * 60 + unbcd(b[4])


def seconds_to_jst_bytes(t: int) -> bytes:
    mjd, rem = divmod(int(t), 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return mjd.to_bytes(2, 'big') + bytes([bcd(h), bcd(m), bcd(s)])


# ---------------------------------------------------------------- SIT parsing

def iter_descriptors(buf: bytes):
    i = 0
    while i + 2 <= len(buf):
        tag, ln = buf[i], buf[i + 1]
        yield tag, buf[i:i + 2 + ln]
        i += 2 + ln


class SitInfo:
    """Parsed SIT contents relevant for NIT/SDT/EIT/TOT synthesis."""

    def __init__(self, sec: bytes):
        tl = ((sec[8] & 0x0F) << 8) | sec[9]
        self.trans_descs = sec[10:10 + tl]
        self.network_id = None
        self.ts_info_desc = b''
        self.ts_name = b''
        for tag, d in iter_descriptors(self.trans_descs):
            if tag == 0xC2 and len(d) >= 9:  # network_identification_descriptor
                self.network_id = int.from_bytes(d[7:9], 'big')
            elif tag == 0xCD:  # TS_information_descriptor
                self.ts_info_desc = d
                name_len = d[3] >> 2
                self.ts_name = d[4:4 + name_len]
        self.services = []
        end = 3 + (((sec[1] & 0x0F) << 8) | sec[2]) - 4
        i = 10 + tl
        while i + 4 <= end:
            sid = int.from_bytes(sec[i:i + 2], 'big')
            dl = ((sec[i + 2] & 0x0F) << 8) | sec[i + 3]
            descs = sec[i + 4:i + 4 + dl]
            i += 4 + dl
            svc = {'sid': sid, 'service_desc': b'', 'event_descs': bytearray(),
                   'event_id': None, 'start': None, 'duration': None, 'jst': None,
                   'service_type': 0x01}
            for tag, d in iter_descriptors(descs):
                if tag == 0x48:
                    svc['service_desc'] = d
                    svc['service_type'] = d[2]
                elif tag == 0xC3:  # partialTS_time_descriptor
                    body = d[2:]
                    svc['start'] = body[1:6]
                    svc['duration'] = body[6:9]
                    if len(body) >= 18 and body[12] & 0x01:  # JST_time_flag
                        svc['jst'] = body[13:18]
                else:
                    if tag == 0xD6 and len(d) >= 3:  # event_group_descriptor
                        count = d[2] & 0x0F
                        for k in range(count):
                            e = d[3 + 4 * k:7 + 4 * k]
                            if len(e) == 4 and int.from_bytes(e[:2], 'big') == sid:
                                svc['event_id'] = int.from_bytes(e[2:], 'big')
                                break
                    svc['event_descs'] += d
            self.services.append(svc)

    def event_key(self):
        return tuple((s['sid'], s['event_id'], s['start'], s['duration'], bytes(s['event_descs']))
                     for s in self.services)


# ---------------------------------------------------------------- table builders

def build_nit(info: SitInfo, tsid: int, onid: int, version: int) -> bytes:
    net_descs = b''
    if info.ts_name:
        net_descs += bytes([0x40, len(info.ts_name)]) + info.ts_name  # network_name
    svc_list = b''.join(s['sid'].to_bytes(2, 'big') + bytes([s['service_type']])
                        for s in info.services)
    ts_descs = info.ts_info_desc + bytes([0x41, len(svc_list)]) + svc_list
    ts_loop = tsid.to_bytes(2, 'big') + onid.to_bytes(2, 'big') + loop_len(len(ts_descs)) + ts_descs
    payload = loop_len(len(net_descs)) + net_descs + loop_len(len(ts_loop)) + ts_loop
    return long_section(0x40, onid, version, 0, 0, payload)


def build_sdt(info: SitInfo, tsid: int, onid: int, version: int) -> bytes:
    body = onid.to_bytes(2, 'big') + b'\xFF'
    for s in info.services:
        d = s['service_desc']
        # EIT_pf flag = 1, running_status = 4 (running), free_CA = 0
        body += s['sid'].to_bytes(2, 'big') + b'\xE1' + bytes([0x80 | (len(d) >> 8), len(d) & 0xFF]) + d
    return long_section(0x42, tsid, version, 0, 0, body)


def build_eit_pf(svc: dict, tsid: int, onid: int, version: int) -> list[bytes]:
    head = tsid.to_bytes(2, 'big') + onid.to_bytes(2, 'big') + bytes([1, 0x4E])
    present = b''
    if svc['start'] is not None:
        d = bytes(svc['event_descs'])
        eid = svc['event_id'] if svc['event_id'] is not None else 0xFFFF
        present = (eid.to_bytes(2, 'big') + svc['start'] + svc['duration']
                   + bytes([0x80 | (len(d) >> 8), len(d) & 0xFF]) + d)
    return [long_section(0x4E, svc['sid'], version, 0, 1, head + present),
            long_section(0x4E, svc['sid'], version, 1, 1, head)]


def build_tot(jst_seconds: int) -> bytes:
    jst = seconds_to_jst_bytes(jst_seconds)
    # local_time_offset_descriptor: JPN, region 0, polarity +, offset 0
    lto = b'JPN' + bytes([0x02, 0x00, 0x00]) + jst + bytes([0x00, 0x00])
    descs = bytes([0x58, len(lto)]) + lto
    body = jst + loop_len(len(descs))
    length = len(body) + len(descs) + 4
    return with_crc(bytes([0x73, 0x70 | (length >> 8), length & 0xFF]) + body + descs)


# ---------------------------------------------------------------- converter

PCR_HZ = 90000
PCR_WRAP = 1 << 33


class Converter:
    def __init__(self):
        self.pk = Packetizer()
        self.tsid = None
        self.info = None
        self.versions = {'nit': 0, 'sdt': 0, 'eit': 0}
        self.tables = {}          # name -> list of (pid, section)
        self.last_emit = {}       # name -> pcr
        self.event_key = None
        self.pcr = None
        self.jst_base = None      # (jst_seconds, pcr_at_that_time)
        self.sit_buf = None
        self.pat_cache = {}
        self.stats = {'packets_in': 0, 'packets_added': 0, 'sit_sections': 0, 'events': 0}

    # --- PAT
    def rewrite_pat(self, pkt: bytes) -> bytes:
        cached = self.pat_cache.get(pkt[4:])
        if cached is not None:
            return pkt[:4] + cached
        payload = pkt[4:]
        afc = (pkt[3] >> 4) & 3
        if afc != 1 or not (pkt[1] & 0x40):
            return pkt
        ptr = payload[0]
        sec = bytearray(payload[1 + ptr:])
        if sec[0] != 0x00:
            return pkt
        length = ((sec[1] & 0x0F) << 8) | sec[2]
        if 3 + length > len(sec):
            return pkt
        self.tsid = int.from_bytes(sec[3:5], 'big')
        for i in range(8, 3 + length - 4, 4):
            if int.from_bytes(sec[i:i + 2], 'big') == 0:
                sec[i + 2] = 0xE0
                sec[i + 3] = 0x10
        sec[3 + length - 4:3 + length] = crc32_mpeg(bytes(sec[:3 + length - 4])).to_bytes(4, 'big')
        new_payload = payload[:1 + ptr] + bytes(sec)
        self.pat_cache[pkt[4:]] = new_payload
        return pkt[:4] + new_payload

    # --- SIT
    def feed_sit(self, pkt: bytes):
        afc = (pkt[3] >> 4) & 3
        if not afc & 1:
            return
        i = 4 + (1 + pkt[4] if afc == 3 else 0)
        if pkt[1] & 0x40:
            self.sit_buf = bytearray(pkt[i + 1 + pkt[i]:])
        elif self.sit_buf is not None:
            self.sit_buf += pkt[i:188]
        else:
            return
        b = self.sit_buf
        if len(b) >= 3:
            length = ((b[1] & 0x0F) << 8) | b[2]
            if len(b) >= 3 + length:
                sec = bytes(b[:3 + length])
                self.sit_buf = None
                if sec[0] == 0x7F and crc32_mpeg(sec[:-4]) == int.from_bytes(sec[-4:], 'big'):
                    self.on_sit(sec)

    def on_sit(self, sec: bytes):
        self.stats['sit_sections'] += 1
        info = SitInfo(sec)
        for s in info.services:
            if s['jst'] is not None and self.pcr is not None:
                self.jst_base = (jst_bytes_to_seconds(s['jst']), self.pcr)
        if self.jst_base is None:
            for s in info.services:
                if s['jst'] is not None:
                    self.jst_base = (jst_bytes_to_seconds(s['jst']), self.pcr or 0)
        tsid = self.tsid if self.tsid is not None else (info.network_id or 0)
        onid = info.network_id if info.network_id is not None else tsid
        first = self.info is None
        key = info.event_key()
        svc_key = tuple((s['sid'], s['service_desc']) for s in info.services)
        old_svc_key = (tuple((s['sid'], s['service_desc']) for s in self.info.services)
                       if self.info else None)
        self.info = info
        if first or svc_key != old_svc_key:
            if not first:
                self.versions['nit'] = (self.versions['nit'] + 1) & 0x1F
                self.versions['sdt'] = (self.versions['sdt'] + 1) & 0x1F
            self.tables['nit'] = [(0x10, build_nit(info, tsid, onid, self.versions['nit']))]
            self.tables['sdt'] = [(0x11, build_sdt(info, tsid, onid, self.versions['sdt']))]
            self.last_emit.pop('nit', None)
            self.last_emit.pop('sdt', None)
        if first or key != self.event_key:
            if not first:
                self.versions['eit'] = (self.versions['eit'] + 1) & 0x1F
            self.stats['events'] += 1
            self.tables['eit'] = [(0x12, s) for svc in info.services
                                  for s in build_eit_pf(svc, tsid, onid, self.versions['eit'])]
            self.last_emit.pop('eit', None)
            self.event_key = key

    # --- scheduling
    def due(self, name: str, interval_s: float) -> bool:
        last = self.last_emit.get(name)
        if last is None or self.pcr is None:
            return True
        return (self.pcr - last) % PCR_WRAP >= interval_s * PCR_HZ

    def emit_tables(self, out: list):
        if self.info is None:
            return
        for name, interval in (('nit', 10), ('sdt', 2), ('eit', 1)):
            if name in self.tables and self.due(name, interval):
                for pid, sec in self.tables[name]:
                    pkts = self.pk.packets(pid, sec)
                    out.extend(pkts)
                    self.stats['packets_added'] += len(pkts)
                self.last_emit[name] = self.pcr
        if self.jst_base is not None and self.pcr is not None and self.due('tot', 5):
            jst0, pcr0 = self.jst_base
            now = jst0 + ((self.pcr - pcr0) % PCR_WRAP) // PCR_HZ
            pkts = self.pk.packets(0x14, build_tot(now))
            out.extend(pkts)
            self.stats['packets_added'] += len(pkts)
            self.last_emit['tot'] = self.pcr

    def convert(self, src: Path, dst: Path):
        with src.open('rb') as f:
            head = f.read(192 * 16)
        if all(len(head) > 4 + 192 * k and head[4 + 192 * k] == 0x47 for k in range(10)):
            size, skip = 192, 4
        elif all(len(head) > 188 * k and head[188 * k] == 0x47 for k in range(10)):
            size, skip = 188, 0
        else:
            raise ValueError('not a 188/192-byte TS')
        chunk_pkts = 20000
        tmp = dst.with_name(dst.name + '.partial')
        with src.open('rb') as f, tmp.open('wb') as w:
            while True:
                data = f.read(size * chunk_pkts)
                if not data:
                    break
                out = []
                for off in range(0, len(data) - size + 1, size):
                    pkt = data[off + skip:off + skip + 188]
                    if pkt[0] != 0x47:
                        continue
                    self.stats['packets_in'] += 1
                    pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
                    afc = (pkt[3] >> 4) & 3
                    if afc & 2 and pkt[4] >= 7 and pkt[5] & 0x10:
                        self.pcr = ((pkt[6] << 25) | (pkt[7] << 17) | (pkt[8] << 9)
                                    | (pkt[9] << 1) | (pkt[10] >> 7))
                    if pid == 0x0000:
                        out.append(self.rewrite_pat(pkt))
                        self.emit_tables(out)
                        continue
                    if pid in (0x0010, 0x0011, 0x0012, 0x0014):
                        continue  # never mix original tables with generated ones
                    if pid == 0x001F:
                        had = self.info is not None
                        self.feed_sit(pkt)
                        out.append(pkt)
                        if not had and self.info is not None:
                            self.emit_tables(out)
                        continue
                    out.append(pkt)
                w.write(b''.join(out))
        tmp.replace(dst)


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ('utf-8', 'utf8'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    ap = argparse.ArgumentParser(description='nasne partial TS -> regular TS (NIT/SDT/EIT/TOT from SIT)')
    ap.add_argument('inputs', nargs='+', type=Path)
    ap.add_argument('-o', '--out-dir', type=Path, help='output folder (default: <input folder>/fixed)')
    ap.add_argument('--overwrite', action='store_true', help='replace existing outputs')
    args = ap.parse_args()
    rc = 0
    for src in args.inputs:
        out_dir = args.out_dir or src.parent / 'fixed'
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / src.name
        if dst.resolve() == src.resolve():
            print(f'skip (output would overwrite input): {src}')
            rc = 1
            continue
        if dst.exists() and not args.overwrite:
            print(f'skip (exists): {dst}')
            continue
        conv = Converter()
        try:
            conv.convert(src, dst)
        except Exception as e:
            print(f'FAILED: {src}: {e}')
            rc = 1
            continue
        s = conv.stats
        print(f'OK: {dst}  packets={s["packets_in"]} added={s["packets_added"]} '
              f'sit={s["sit_sections"]} events={s["events"]}')
    return rc


if __name__ == '__main__':
    sys.exit(main())
