""".NET BinaryWriter / BinaryReader emulation for the Hex protocol."""

import struct
import io


class BinaryWriter:
    """Emulate .NET BinaryWriter for Hex protocol."""

    def __init__(self):
        self.buf = bytearray()

    def write_int32(self, v: int) -> None:
        self.buf.extend(struct.pack('<i', v))

    def write_uint64(self, v: int) -> None:
        self.buf.extend(struct.pack('<Q', v))

    def write_int64(self, v: int) -> None:
        self.buf.extend(struct.pack('<q', v))

    def write_bool(self, v: bool) -> None:
        self.buf.append(1 if v else 0)

    def write_string(self, s: str) -> None:
        """BinaryWriter.Write(string): 7-bit-encoded length + UTF-8."""
        utf8_bytes = s.encode('utf-8')
        self._write_7bit_int(len(utf8_bytes))
        self.buf.extend(utf8_bytes)

    def write_raw_bytes(self, b: bytes) -> None:
        self.buf.extend(b)

    def get_bytes(self) -> bytes:
        return bytes(self.buf)

    def _write_7bit_int(self, value: int) -> None:
        while value >= 0x80:
            self.buf.append((value | 0x80) & 0xFF)
            value >>= 7
        self.buf.append(value & 0xFF)


class BinaryReader:
    """Emulate .NET BinaryReader for Hex protocol."""

    def __init__(self, data: bytes):
        self.buf = io.BytesIO(data)

    def read_int32(self) -> int:
        return struct.unpack('<i', self.buf.read(4))[0]

    def read_uint64(self) -> int:
        return struct.unpack('<Q', self.buf.read(8))[0]

    def read_int64(self) -> int:
        return struct.unpack('<q', self.buf.read(8))[0]

    def read_bool(self) -> bool:
        return self.buf.read(1)[0] != 0

    def read_string(self) -> str:
        length = self._read_7bit_int()
        return self.buf.read(length).decode('utf-8')

    def read_bytes(self, count: int) -> bytes:
        return self.buf.read(count)

    def _read_7bit_int(self) -> int:
        result = 0
        shift = 0
        while True:
            b = self.buf.read(1)[0]
            result |= (b & 0x7F) << shift
            if (b & 0x80) == 0:
                break
            shift += 7
        return result
