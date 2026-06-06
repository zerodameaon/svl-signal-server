import logging
import socket
import time
from threading import Thread, RLock

from enums import *
from layout_handle import LayoutHandle

_LCC_MTI_DESCRIPTIONS = {
    '0x100': 'Initialization Complete (Full)',
    '0x101': 'Initialization Complete (Simple)',
    '0x488': 'Verify NodeID (Addressed)',
    '0x490': 'Verify NodeID (Global)',
    '0x170': 'Verified NodeID (Full)',
    '0x171': 'Verified NodeID (Simple)',
    '0x68':  'Optional Interaction Rejected',
    '0xa8':  'Terminate due to Error',
    '0x4a4': 'Consumer Range Identified',
    '0x4c4': 'Consumer Identified and State=Valid',
    '0x4c5': 'Consumer Identified and State=Invalid',
    '0x4c7': 'Consumer Identified and State=Unknown',
    '0x524': 'Producer Range Identified',
    '0x544': 'Producer Identified and State=Valid',
    '0x545': 'Producer Identified and State=Invalid',
    '0x547': 'Producer Identified and State=Unknown',
    '0x5b4': 'P/C Event Report',
    '0x668': 'Protocol Support Reply',
    '0x828': 'Protocol Support Inquiry',
    '0x8f4': 'Identify Consumer',
    '0x914': 'Identify Producer',
    '0xa08': 'SNIP Reply',
    '0xde8': 'SNIP Request',
}


class OpenlcbLayoutHandle(LayoutHandle):

    def __init__(self, openlcb_network):
        self._network = openlcb_network
        self._s_lock = RLock()
        self._s = None
        self._InitSocket()
        # {mast_name -> (first_eventid, appearance)}
        self._cache = {}
        self._last_broadcast_time = time.time()
        self._rcv_data = ''
        self._recv_thread = Thread(target=self._CheckForIncomingLCCData)
        self._recv_thread.daemon = True
        self._recv_thread.start()

    def _InitSocket(self) -> None:
        logging.info('Initializing OpenLCB Hub socket')
        try:
            if self._s:
                del self._s
            self._s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._s.settimeout(1.0)
            self._s.connect(('localhost', 12021))
        except Exception:
            logging.exception('Error initializing OLCB socket')

    def _RemoveJunk(self, eventid: str) -> str:
        return eventid.replace(' ', '').replace(':', '').replace('.', '')

    def _CheckForIncomingLCCData(self) -> None:
        while True:
            with self._s_lock:
                if self._s is None:
                    time.sleep(1)
                    continue
                try:
                    logging.debug('Checking for LCC data...')
                    data = self._s.recv(4096)
                    logging.debug('got data: "%s"', data)
                    self._rcv_data += data.decode('utf-8', errors='replace')
                except socket.timeout:
                    pass
                except Exception:
                    logging.exception('LCC data check failed')

            while True:
                self._rcv_data = self._rcv_data.lstrip()
                logging.debug('In recv queue: "%s"', self._rcv_data)
                semicolon_idx = self._rcv_data.find(';')
                if semicolon_idx == -1:
                    logging.debug('Recv buffer does not contain an end of frame')
                    break
                if not self._rcv_data.startswith(':X'):
                    self._rcv_data = self._rcv_data[semicolon_idx + 1:]
                    continue
                packet = self._rcv_data[:semicolon_idx]
                self._ProcessCANPacket(packet)
                self._rcv_data = self._rcv_data[semicolon_idx + 1:]

            time.sleep(1)

    def _ProcessCANPacket(self, packet: str) -> None:
        logging.debug('processing packet: "%s"', packet)
        packet = packet[2:]  # strip ':X' prefix (semicolon already stripped by caller)
        logging.debug('Full packet: "%s"', packet)
        packet_parts = packet.split('N')
        if len(packet_parts) != 2:
            logging.error('Ignoring invalid packet')
            return
        header, data = packet_parts
        logging.debug('Header: "%s" Data: "%s"', header, data)
        header_bin = bin(int(header, 16))[2:]
        logging.debug('Header binary: %s', header_bin)
        logging.debug('Header hex: %s', hex(int(header_bin, 2)))
        if len(header_bin) != 29:
            logging.error('Ignoring invalid-len header')
            return
        if header_bin[1] != '1':
            logging.debug('Ignoring non-openlcb frame')
            return
        hdr_frame_type = int(header_bin[2:5], 2)
        logging.debug('Frame type: %s', hdr_frame_type)
        if hdr_frame_type in [2, 3, 4, 5]:
            logging.debug('Ignoring datagram frame (type %s)', hdr_frame_type)
            return
        elif hdr_frame_type in [0, 6]:
            logging.debug('Ignoring reserved frame (type %s)', hdr_frame_type)
            return
        elif hdr_frame_type == 7:
            logging.debug('Ignoring stream frame (type %s)', hdr_frame_type)
            return
        elif hdr_frame_type != 1:
            logging.debug('Ignoring unknown frame (type %s)', hdr_frame_type)
            return
        can_mti_bin = header_bin[5:17]
        logging.debug('binary mti: %s', can_mti_bin)
        can_mti = hex(int(can_mti_bin, 2))
        description = _LCC_MTI_DESCRIPTIONS.get(can_mti, f'Unknown Packet with MTI {can_mti}')
        logging.info('Processing incoming packet (%s)', description)
        if can_mti not in ['0x100', '0x101']:
            return
        logging.info('Broadcasting cache!')
        self._BroadcastCache()

    def SetTriLightSignalHeadAppearance(self, mast_name: str, head_first_eventid: str, appearance: str, ignore_cache: bool = False) -> None:
        if not ignore_cache:
            if self._cache.get(mast_name) == (head_first_eventid, appearance):
                logging.debug('  Aspect of %s is already %s', mast_name, appearance)
                return
        event_offset = {
            HEAD_GREEN: 0,
            HEAD_YELLOW: 1,
            HEAD_RED: 2,
            HEAD_FLASHING_GREEN: 3,
            HEAD_FLASHING_YELLOW: 4,
            HEAD_FLASHING_RED: 5,
            HEAD_DARK: 6,
        }.get(appearance, 6)

        first_eventid = self._RemoveJunk(head_first_eventid)
        logging.debug("  Head's first EventId: %s", first_eventid)
        appearance_eventid = hex(int(first_eventid, base=16) + event_offset)[2:].upper()
        appearance_eventid = appearance_eventid.rjust(len(first_eventid), '0')
        logging.debug('  Appearance eventid: %s (first+%s)', appearance_eventid, event_offset)

        can_frame = f':X195B46ADN{self._RemoveJunk(appearance_eventid)};\n'
        self._Send(can_frame)
        self._cache[mast_name] = (head_first_eventid, appearance)

    def SetLampAppearance(self, lamp_first_eventid: str, appearance: str, ignore_cache: bool = False) -> None:
        assert appearance in ['ON', 'FLASHING', 'OFF']

        if not ignore_cache:
            if self._cache.get(lamp_first_eventid) == appearance:
                logging.debug('  Appearance of lamp at address %s is already %s', lamp_first_eventid, appearance)
                return

        logging.debug('CPL Appearance')
        event_offset = {'ON': 0, 'FLASHING': 1, 'OFF': 2}.get(appearance, 2)

        first_eventid = self._RemoveJunk(lamp_first_eventid)
        logging.debug("  Lamp's first EventId: %s", first_eventid)
        appearance_eventid = hex(int(first_eventid, base=16) + event_offset)[2:].upper()
        appearance_eventid = appearance_eventid.rjust(len(first_eventid), '0')
        logging.debug('  Appearance eventid: %s (first+%s)', appearance_eventid, event_offset)

        can_frame = f':X195B46ADN{self._RemoveJunk(appearance_eventid)};\n'
        self._Send(can_frame)
        self._cache[lamp_first_eventid] = appearance

    def _Send(self, frame: str) -> None:
        with self._s_lock:
            logging.info('  Sending LCC CAN packet %s', frame)
            try:
                self._s.sendall(frame.encode())
            except Exception:
                logging.exception('Send to socket failed')
                self._InitSocket()

    def _BroadcastCache(self) -> None:
        logging.info('Time to rebroadcast LCC cache')
        for mast_name, (first_eventid, appearance) in self._cache.items():
            self.SetTriLightSignalHeadAppearance(mast_name, first_eventid, appearance, ignore_cache=True)
