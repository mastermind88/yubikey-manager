# Copyright (c) 2015 Yubico AB
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

from __future__ import absolute_import

import six
import struct
import logging
from .util import AID, Tlv
from .driver_ccid import (APDUError, SW, GP_INS_SELECT)
from enum import Enum, IntEnum, unique
from binascii import b2a_hex
from collections import namedtuple
from cryptography import x509
from cryptography.utils import int_to_bytes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.hazmat.primitives.asymmetric import rsa, ec


logger = logging.getLogger(__name__)


_KeySlot = namedtuple('KeySlot', [
    'value',
    'index',
    'key_id',
    'fingerprint',
    'gen_time',
    'uif',  # touch policy
    'crt'  # Control Reference Template
])


@unique
class KEY_SLOT(_KeySlot, Enum):  # noqa: N801
    SIG = _KeySlot('SIGNATURE', 1, 0xc1, 0xc7, 0xce, 0xd6, Tlv(0xb6))
    ENC = _KeySlot('ENCRYPTION', 2, 0xc2, 0xc8, 0xcf, 0xd7, Tlv(0xb8))
    AUT = _KeySlot('AUTHENTICATION', 3, 0xc3, 0xc9, 0xd0, 0xd8, Tlv(0xa4))
    ATT = _KeySlot('ATTESTATION', 4, 0xda, 0xdb, 0xdd, 0xd9,
                   Tlv(0xb6, Tlv(0x84, b'\x81')))


@unique
class TOUCH_MODE(IntEnum):  # noqa: N801
    OFF = 0x00
    ON = 0x01
    FIXED = 0x02
    CACHED = 0x03
    CACHED_FIXED = 0x04

    def __str__(self):
        if self == TOUCH_MODE.OFF:
            return 'Off'
        elif self == TOUCH_MODE.ON:
            return 'On'
        elif self == TOUCH_MODE.FIXED:
            return 'On (fixed)'
        elif self == TOUCH_MODE.CACHED:
            return 'Cached'
        elif self == TOUCH_MODE.CACHED_FIXED:
            return 'Cached (fixed)'


@unique
class INS(IntEnum):  # noqa: N801
    GET_DATA = 0xca
    GET_VERSION = 0xf1
    SET_PIN_RETRIES = 0xf2
    VERIFY = 0x20
    TERMINATE = 0xe6
    ACTIVATE = 0x44
    PUT_DATA = 0xda
    PUT_DATA_ODD = 0xdb
    GET_ATTESTATION = 0xfb
    SEND_REMAINING = 0xc0
    SELECT_DATA = 0xa5


PinRetries = namedtuple('PinRetries', ['pin', 'reset', 'admin'])


PW1 = 0x81
PW3 = 0x83
INVALID_PIN = b'\0'*8
TOUCH_METHOD_BUTTON = 0x20


@unique
class DO(IntEnum):
    AID = 0x4f
    PW_STATUS = 0xc4
    CARDHOLDER_CERTIFICATE = 0x7f21
    ATT_CERTIFICATE = 0xfc


@unique
class OID(bytes, Enum):
    SECP256R1 = b'\x2a\x86\x48\xce\x3d\x03\x01\x07',
    SECP384R1 = b'\x2b\x81\x04\x00\x22',
    SECP521R1 = b'\x2b\x81\x04\x00\x23',
    X25519 = b'\x2b\x06\x01\x04\x01\x97\x55\x01\x05\x01'
    ED25519 = b'\x2b\x06\x01\x04\x01\xda\x47\x0f\x01'
    # TODO: Add more curves

    @classmethod
    def for_name(cls, name):
        return getattr(cls, name.upper())


def _get_key_attributes(key, key_slot):
    if isinstance(key, rsa.RSAPrivateKey):
        if key.private_numbers().public_numbers.e != 65537:
            raise ValueError('RSA keys with e != 65537 are not supported!')
        return struct.pack('>BHHB', 0x01, key.key_size, 32, 0)
    if isinstance(key, ec.EllipticCurvePrivateKey):
        if key.curve.name in ('x25519', 'ed25519'):
            algorithm = b'\x16'
        elif key_slot == KEY_SLOT.ENC:
            algorithm = b'\x12'
        else:
            algorithm = b'\x13'
        return algorithm + OID.for_name(key.curve.name)
    raise ValueError('Not a valid private key!')


def _get_key_template(key, key_slot, crt=False):

    def _pack_tlvs(tlvs):
        header = b''
        body = b''
        for tlv in tlvs:
            header += tlv[:-tlv.length]
            body += tlv.value
        return Tlv(0x7f48, header) + Tlv(0x5f48, body)

    private_numbers = key.private_numbers()

    if isinstance(key, rsa.RSAPrivateKey):
        ln = (key.key_size // 8) // 2

        e = Tlv(0x91, b'\x01\x00\x01')  # e=65537
        p = Tlv(0x92, int_to_bytes(private_numbers.p, ln))
        q = Tlv(0x93, int_to_bytes(private_numbers.q, ln))
        values = (e, p, q)
        if crt:
            dp = Tlv(0x94, int_to_bytes(private_numbers.dmp1, ln))
            dq = Tlv(0x95, int_to_bytes(private_numbers.dmq1, ln))
            qinv = Tlv(0x96, int_to_bytes(private_numbers.iqmp, ln))
            n = Tlv(0x97, int_to_bytes(private_numbers.public_numbers.n, 2*ln))
            values += (dp, dq, qinv, n)

    elif isinstance(key, ec.EllipticCurvePrivateKey):
        ln = key.key_size // 8

        privkey = Tlv(0x92, int_to_bytes(private_numbers.private_value, ln))
        values = (privkey,)

    return Tlv(0x4d, key_slot.crt + _pack_tlvs(values))


class OpgpController(object):

    def __init__(self, driver):
        self._driver = driver
        # Use send_apdu instead of driver.select()
        # to get OpenPGP specific error handling.
        self.send_apdu(0, GP_INS_SELECT, 0x04, 0, AID.OPGP)
        self._version = self._read_version()

    @property
    def version(self):
        return self._version

    def send_apdu(self, cl, ins, p1, p2, data=b'', check=SW.OK):
        try:
            return self._driver.send_apdu(cl, ins, p1, p2, data, check)
        except APDUError as e:
            # If OpenPGP is in a terminated state send activate.
            if e.sw in (SW.NO_INPUT_DATA, SW.CONDITIONS_NOT_SATISFIED):
                self._driver.send_apdu(0, INS.ACTIVATE, 0, 0)
                return self._driver.send_apdu(cl, ins, p1, p2, data, check)
            raise

    def send_cmd(self, cl, ins, p1=0, p2=0, data=b'', check=SW.OK):
        while len(data) > 0xff:
            self._driver.send_apdu(0x10, ins, p1, p2, data[:0xff])
            data = data[0xff:]
        resp, sw = self._driver.send_apdu(0, ins, p1, p2, data, check=None)

        while (sw >> 8) == SW.MORE_DATA:
            more, sw = self._driver.send_apdu(
                0, INS.SEND_REMAINING, 0, 0, b'', check=None)
            resp += more

        if check is None:
            return resp, sw
        elif sw != check:
            raise APDUError(resp, sw)
        return resp

    def _get_data(self, do):
        return self.send_cmd(0, INS.GET_DATA, do >> 8, do & 0xff)

    def _put_data(self, do, data):
        self.send_cmd(0, INS.PUT_DATA, do >> 8, do & 0xff, data)

    def _select_certificate(self, key_slot):
        self.send_cmd(
            0, INS.SELECT_DATA, 3 - key_slot.index, 0x04,
            Tlv(0, Tlv(0x60, Tlv(0x5c, b'\x7f\x21')))[1:]
        )

    def _read_version(self):
        bcd_hex = b2a_hex(self.send_apdu(0, INS.GET_VERSION, 0, 0))
        return tuple(int(bcd_hex[i:i+2]) for i in range(0, 6, 2))

    def get_openpgp_version(self):
        data = self._get_data(DO.AID)
        return (six.indexbytes(data, 6), six.indexbytes(data, 7))

    def get_remaining_pin_tries(self):
        data = self._get_data(DO.PW_STATUS)
        return PinRetries(*six.iterbytes(data[4:7]))

    def _block_pins(self):
        retries = self.get_remaining_pin_tries()

        for _ in range(retries.pin):
            self.send_apdu(0, INS.VERIFY, 0, PW1, INVALID_PIN, check=None)
        for _ in range(retries.admin):
            self.send_apdu(0, INS.VERIFY, 0, PW3, INVALID_PIN, check=None)

    def reset(self):
        if self.version < (1, 0, 6):
            raise ValueError('Resetting OpenPGP data requires version 1.0.6 or '
                             'later.')
        self._block_pins()
        self.send_apdu(0, INS.TERMINATE, 0, 0)
        self.send_apdu(0, INS.ACTIVATE, 0, 0)

    def _verify(self, pw, pin):
        try:
            pin = pin.encode('utf-8')
            self.send_apdu(0, INS.VERIFY, 0, pw, pin)
        except APDUError:
            pw_remaining = self.get_remaining_pin_tries()[pw-PW1]
            raise ValueError('Invalid PIN, {} tries remaining.'.format(
                pw_remaining))

    @property
    def supported_touch_policies(self):
        if self.version < (4, 2, 0):
            return []
        if self.version < (5, 2, 1):
            return [TOUCH_MODE.ON, TOUCH_MODE.OFF, TOUCH_MODE.FIXED]
        if self.version >= (5, 2, 1):
            return [
                TOUCH_MODE.ON, TOUCH_MODE.OFF, TOUCH_MODE.FIXED,
                TOUCH_MODE.CACHED, TOUCH_MODE.CACHED_FIXED]

    @property
    def supports_attestation(self):
        return self.version >= (5, 2, 1)

    def get_touch(self, key_slot):
        if not self.supported_touch_policies:
            raise ValueError('Touch policy is available on YubiKey 4 or later.')
        if key_slot == KEY_SLOT.ATT and not self.supports_attestation:
            raise ValueError('Attestation key not available on this device.')
        data = self._get_data(key_slot.uif)
        return TOUCH_MODE(six.indexbytes(data, 0))

    def set_touch(self, key_slot, mode, admin_pin):
        if not self.supported_touch_policies:
            raise ValueError('Touch policy is available on YubiKey 4 or later.')
        if mode not in self.supported_touch_policies:
            raise ValueError('Touch policy not available on this device.')
        self._verify(PW3, admin_pin)
        self._put_data(key_slot.uif,
                       bytes(bytearray([mode, TOUCH_METHOD_BUTTON])))

    def set_pin_retries(self, pw1_tries, pw2_tries, pw3_tries, admin_pin):
        if self.version < (1, 0, 7):  # For YubiKey NEO
            raise ValueError('Setting PIN retry counters requires version '
                             '1.0.7 or later.')
        if (4, 0, 0) <= self.version < (4, 3, 1):  # For YubiKey 4
            raise ValueError('Setting PIN retry counters requires version '
                             '4.3.1 or later.')
        self._verify(PW3, admin_pin)
        self.send_apdu(0, INS.SET_PIN_RETRIES, 0, 0,
                       bytes(bytearray([pw1_tries, pw2_tries, pw3_tries])))

    def read_certificate(self, key_slot):
        if key_slot == KEY_SLOT.ATT:
            data = self._get_data(DO.ATT_CERTIFICATE)
        else:
            self._select_certificate(key_slot)
            data = self._get_data(DO.CARDHOLDER_CERTIFICATE)
        if not data:
            raise ValueError('No certificate found!')
        return x509.load_der_x509_certificate(data, default_backend())

    def import_certificate(self, key_slot, certificate, admin_pin):
        self._verify(PW3, admin_pin)
        cert_data = certificate.public_bytes(Encoding.DER)
        if key_slot == KEY_SLOT.ATT:
            self._put_data(DO.ATT_CERTIFICATE, cert_data)
        else:
            self._select_certificate(key_slot)
            self._put_data(DO.CARDHOLDER_CERTIFICATE, cert_data)

    def import_key(self, key_slot, key, admin_pin, fingerprint=None,
                   timestamp=None):
        self._verify(PW3, admin_pin)

        attributes = _get_key_attributes(key, key_slot)
        self._put_data(key_slot.key_id, attributes)

        template = _get_key_template(key, key_slot, self.version < (4, 0, 0))
        self.send_cmd(0, INS.PUT_DATA_ODD, 0x3f, 0xff, template)

        if fingerprint is not None:
            self._put_data(key_slot.fingerprint, fingerprint)

        if timestamp is not None:
            self._put_data(key_slot.gen_time, struct.pack('>I', timestamp))

    def import_attestation_key(self, key, admin_pin):
        self.import_key(KEY_SLOT.ATT, key, admin_pin)

    def delete_key(self, key_slot, admin_pin):
        self._verify(PW3, admin_pin)
        # Delete key by changing the key attributes twice.
        self._put_data(key_slot.key_id, struct.pack('>BHHB', 0x01, 2048, 32, 0))
        self._put_data(key_slot.key_id, struct.pack('>BHHB', 0x01, 4096, 32, 0))

    def delete_attestation_key(self, admin_pin):
        self.delete_key(KEY_SLOT.ATT, admin_pin)

    def delete_certificate(self, key_slot, admin_pin):
        self._verify(PW3, admin_pin)
        if key_slot == KEY_SLOT.ATT:
            self._put_data(DO.ATT_CERTIFICATE, b'')
        else:
            self._select_certificate(key_slot)
            self._put_data(DO.CARDHOLDER_CERTIFICATE, b'')

    def attest(self, key_slot, pin):
        self._verify(PW1, pin)
        self.send_apdu(0x80, INS.GET_ATTESTATION, key_slot.index, 0)
        return self.read_certificate(key_slot)
