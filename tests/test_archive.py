"""Byte-level decoding: archives, the .Z codec, delimited and HTML tables."""

from __future__ import annotations

import bz2
import gzip
import io
import zipfile
from collections.abc import Sequence

import pytest

from psx_desk.dataflows.archive import (
    BitReader,
    decode_unix_compress,
    decompress,
    html_tables,
    html_text,
    map_columns,
    normalize_header,
    parse_table,
    sniff,
    table_as_records,
    text_of,
    zip_members,
)
from psx_desk.dataflows.http import PayloadRejected

# --------------------------------------------------------------------------- #
# A raw Unix-compress code writer, so the decoder is tested against exact bytes.
# --------------------------------------------------------------------------- #


class _BitWriter:
    """Pack fixed-width codes least-significant-bit first (the .Z wire order)."""

    def __init__(self) -> None:
        self.value = 0
        self.bits = 0
        self.out = bytearray()

    def write(self, code: int, width: int) -> None:
        self.value |= code << self.bits
        self.bits += width
        while self.bits >= 8:
            self.out.append(self.value & 0xFF)
            self.value >>= 8
            self.bits -= 8

    def flush(self) -> bytes:
        if self.bits:
            self.out.append(self.value & 0xFF)
        return bytes(self.out)


def z_stream(codes: Sequence[int], *, max_bits: int = 12, block_mode: bool = True) -> bytes:
    flags = max_bits | (0x80 if block_mode else 0)
    writer = _BitWriter()
    for code in codes:
        writer.write(code, 9)
    return b"\x1f\x9d" + bytes([flags]) + writer.flush()


def test_sniff_identifies_every_format_the_portal_serves():
    assert sniff(gzip.compress(b"x")) == "gzip"
    assert sniff(bz2.compress(b"x")) == "bz2"
    assert sniff(z_stream([ord("S")])) == "compress"
    assert sniff(b"PK\x03\x04rest") == "zip"
    assert sniff(b"%PDF-1.7") == "pdf"
    assert sniff(b"<!DOCTYPE html><html>") == "html"
    assert sniff(b'[{"ts": 1}]') == "json"
    assert sniff(b"") == "empty"


def test_unix_compress_decodes_literals_and_a_back_reference():
    # "S", "Y", then code 257, which the dictionary defines as "SY" once the
    # second literal has been added -> "SYSY".
    stream = z_stream([ord("S"), ord("Y"), 257])
    assert decode_unix_compress(stream) == b"SYSY"


def test_unix_compress_honours_a_clear_code():
    stream = z_stream([ord("S"), 256, ord("T")])
    assert decode_unix_compress(stream) == b"ST"


def test_unix_compress_rejects_an_over_long_code():
    stream = z_stream([ord("S"), 900])
    with pytest.raises(PayloadRejected):
        decode_unix_compress(stream)


def test_system_decoder_is_tried_first_and_fails_softly():
    from psx_desk.dataflows.archive import external_uncompress

    # Not a valid .Z stream: the platform tool must decline, not raise.
    assert external_uncompress(b"\x1f\x9d\x0c" + b"garbage" * 4) in (None, b"")


def test_compress_rejects_a_foreign_stream():
    with pytest.raises(PayloadRejected):
        decode_unix_compress(b"NOTAZ")


def test_decompress_handles_gzip_bz2_and_plain():
    assert decompress(gzip.compress(b"hello")) == b"hello"
    assert decompress(bz2.compress(b"hello")) == b"hello"
    assert decompress(b"plain,csv") == b"plain,csv"


def test_text_of_survives_cp1252_and_utf8():
    assert "Rupee" in text_of("Rupee,PKR".encode("cp1252"))
    assert text_of("₨,PKR".encode()) == "₨,PKR"


def test_zip_members_and_bad_archive():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.csv", "SYMBOL,CLOSE\nSYS,1\n")
        archive.writestr("folder/", "")
    members = zip_members(buffer.getvalue())
    assert list(members) == ["a.csv"]
    with pytest.raises(PayloadRejected):
        zip_members(b"PK\x03\x04broken")


def test_header_normalisation_keeps_percent_change_distinct():
    assert normalize_header("LTP") == "ltp"
    assert normalize_header("%CHANGE") == "pct_change"
    assert normalize_header("CHANGE %") == "change_pct"
    assert normalize_header("") == "col"


def test_map_columns_picks_the_psx_spellings():
    mapping = map_columns(["SYMBOL", "CURRENT", "CHANGE", "%CHANGE", "VOLUME", "VALUE (mn)"])
    assert mapping[0] == "symbol"
    assert mapping[1] == "close"
    assert mapping[3] == "pct_change"
    assert mapping[5].startswith("value")


def test_parse_table_skips_a_title_line():
    table = parse_table(
        "Daily Quotations\nSYMBOL,OPEN,HIGH,LOW,CLOSE,VOLUME\n"
        "SYS,410,415,408,412.55,1200000\nENGRO,280,290,279,285.10,900000\n"
    )
    assert table.headers[:2] == ["symbol", "open"]
    assert len(table) == 2
    assert table.rows[0]["close"] == "412.55"
    assert table.find_symbol_column() == "symbol"


def test_parse_table_rejects_a_header_only_payload():
    with pytest.raises(PayloadRejected):
        parse_table("SYMBOL,CLOSE\n")


def test_html_tables_and_text():
    html = "<html><body><h1>Defaulting Companies</h1><table><tr><th>SYMBOL</th><th>CLOSE</th></tr><tr><td>SYS</td><td>412.55</td></tr></table></body></html>"
    tables = html_tables(html)
    assert len(tables) == 1
    records = table_as_records(tables[0])
    assert records.rows[0]["symbol"] == "SYS"
    assert "Defaulting Companies" in html_text(html)


def test_bit_reader_reads_lsb_first():
    # 0b1010_1010 and 0b0000_0011, packed LSB-first at 5+3 bits.
    reader = BitReader(bytes([0xAA, 0x03]) + b"\x00")
    assert reader.read(5) == 0b01010
    assert reader.read(3) == 0b101
