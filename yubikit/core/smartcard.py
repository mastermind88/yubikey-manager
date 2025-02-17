# Copyright (c) 2020 Yubico AB
# All rights reserved.
#
#   Redistribution and use in source and binary forms, with or
#   without modification, are permitted provided that the following
#   conditions are met:
#
#    1. Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#    2. Redistributions in binary form must reproduce the above
#       copyright notice, this list of conditions and the following
#       disclaimer in the documentation and/or other materials provided
#       with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from . import (
    Version,
    TRANSPORT,
    USB_INTERFACE,
    Connection,
    CommandError,
    ApplicationNotAvailableError,
)
from time import time
from enum import Enum, IntEnum, unique
from typing import Tuple
import abc
import struct
import logging

logger = logging.getLogger(__name__)


class ApduError(CommandError):
    """Thrown when an APDU response has the wrong SW code"""

    def __init__(self, data: bytes, sw: int):
        self.data = data
        self.sw = sw

    def __str__(self):
        return f"APDU error: SW=0x{self.sw:04x}"


@unique
class ApduFormat(str, Enum):
    """APDU encoding format"""

    SHORT = "short"
    EXTENDED = "extended"


@unique
class AID(bytes, Enum):
    """YubiKey Application smart card AID values."""

    OTP = bytes.fromhex("a0000005272001")
    MANAGEMENT = bytes.fromhex("a000000527471117")
    OPENPGP = bytes.fromhex("d27600012401")
    OATH = bytes.fromhex("a0000005272101")
    PIV = bytes.fromhex("a000000308")
    FIDO = bytes.fromhex("a0000006472f0001")
    HSMAUTH = bytes.fromhex("a000000527210701")


@unique
class SW(IntEnum):
    NO_INPUT_DATA = 0x6285
    VERIFY_FAIL_NO_RETRY = 0x63C0
    WRONG_LENGTH = 0x6700
    SECURITY_CONDITION_NOT_SATISFIED = 0x6982
    AUTH_METHOD_BLOCKED = 0x6983
    DATA_INVALID = 0x6984
    CONDITIONS_NOT_SATISFIED = 0x6985
    COMMAND_NOT_ALLOWED = 0x6986
    INCORRECT_PARAMETERS = 0x6A80
    FUNCTION_NOT_SUPPORTED = 0x6A81
    FILE_NOT_FOUND = 0x6A82
    NO_SPACE = 0x6A84
    REFERENCE_DATA_NOT_FOUND = 0x6A88
    APPLET_SELECT_FAILED = 0x6999
    WRONG_PARAMETERS_P1P2 = 0x6B00
    INVALID_INSTRUCTION = 0x6D00
    COMMAND_ABORTED = 0x6F00
    OK = 0x9000


class SmartCardConnection(Connection, metaclass=abc.ABCMeta):
    usb_interface = USB_INTERFACE.CCID

    @property
    @abc.abstractmethod
    def transport(self) -> TRANSPORT:
        """Get the transport type of the connection (USB or NFC)"""

    @abc.abstractmethod
    def send_and_receive(self, apdu: bytes) -> Tuple[bytes, int]:
        """Sends a command APDU and returns the response"""


INS_SELECT = 0xA4
P1_SELECT = 0x04
P2_SELECT = 0x00

INS_SEND_REMAINING = 0xC0
SW1_HAS_MORE_DATA = 0x61

SHORT_APDU_MAX_CHUNK = 0xFF


def _encode_short_apdu(cla, ins, p1, p2, data, le=0):
    buf = struct.pack(">BBBBB", cla, ins, p1, p2, len(data)) + data
    if le:
        buf += struct.pack(">B", le)
    return buf


def _encode_extended_apdu(cla, ins, p1, p2, data, le=0):
    buf = struct.pack(">BBBBBH", cla, ins, p1, p2, 0, len(data)) + data
    if le:
        buf += struct.pack(">H", le)
    return buf


class SmartCardProtocol:
    """An implementation of the Smart Card protocol."""

    def __init__(
        self,
        smartcard_connection: SmartCardConnection,
        ins_send_remaining: int = INS_SEND_REMAINING,
    ):
        self.apdu_format = ApduFormat.SHORT
        self.connection = smartcard_connection
        self._ins_send_remaining = ins_send_remaining
        self._touch_workaround = False
        self._last_long_resp = 0.0

    def close(self) -> None:
        self.connection.close()

    def enable_touch_workaround(self, version: Version) -> None:
        self._touch_workaround = self.connection.transport == TRANSPORT.USB and (
            (4, 2, 0) <= version <= (4, 2, 6)
        )
        logger.debug(f"Touch workaround enabled={self._touch_workaround}")

    def select(self, aid: bytes) -> bytes:
        """Perform a SELECT instruction.

        :param aid: The YubiKey application AID value.
        """
        try:
            return self.send_apdu(0, INS_SELECT, P1_SELECT, P2_SELECT, aid)
        except ApduError as e:
            if e.sw in (
                SW.FILE_NOT_FOUND,
                SW.APPLET_SELECT_FAILED,
                SW.INVALID_INSTRUCTION,
                SW.WRONG_PARAMETERS_P1P2,
            ):
                raise ApplicationNotAvailableError()
            raise

    def send_apdu(
        self, cla: int, ins: int, p1: int, p2: int, data: bytes = b"", le: int = 0
    ) -> bytes:
        """Send APDU message.

        :param cla: The instruction class.
        :param ins: The instruction code.
        :param p1: The instruction parameter.
        :param p2: The instruction parameter.
        :param data: The command data in bytes.
        :param le: The maximum number of bytes in the data
            field of the response.
        """
        if (
            self._touch_workaround
            and self._last_long_resp > 0
            and time() - self._last_long_resp < 2
        ):
            logger.debug("Sending dummy APDU as touch workaround")
            self.connection.send_and_receive(
                _encode_short_apdu(0, 0, 0, 0, b"")
            )  # Dummy APDU, returns error
            self._last_long_resp = 0

        if self.apdu_format is ApduFormat.SHORT:
            while len(data) > SHORT_APDU_MAX_CHUNK:
                chunk, data = data[:SHORT_APDU_MAX_CHUNK], data[SHORT_APDU_MAX_CHUNK:]
                response, sw = self.connection.send_and_receive(
                    _encode_short_apdu(0x10 | cla, ins, p1, p2, chunk, le)
                )
                if sw != SW.OK:
                    raise ApduError(response, sw)
            response, sw = self.connection.send_and_receive(
                _encode_short_apdu(cla, ins, p1, p2, data, le)
            )
            get_data = _encode_short_apdu(0, self._ins_send_remaining, 0, 0, b"")
        elif self.apdu_format is ApduFormat.EXTENDED:
            response, sw = self.connection.send_and_receive(
                _encode_extended_apdu(cla, ins, p1, p2, data, le)
            )
            get_data = _encode_extended_apdu(0, self._ins_send_remaining, 0, 0, b"")
        else:
            raise TypeError("Invalid ApduFormat set")

        # Read chained response
        buf = b""
        while sw >> 8 == SW1_HAS_MORE_DATA:
            buf += response
            response, sw = self.connection.send_and_receive(get_data)

        if sw != SW.OK:
            raise ApduError(response, sw)
        buf += response

        if self._touch_workaround and len(buf) > 54:
            self._last_long_resp = time()
        else:
            self._last_long_resp = 0

        return buf
