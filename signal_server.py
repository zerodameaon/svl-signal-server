import logging
import logging.handlers
import argparse
import signal
import sys
from dataclasses import dataclass
from enums import *
from signal_requirements import *
import signal_config
import jmri
from openlcb import OpenlcbLayoutHandle
import os
import time
import prettytable
from lxml import etree
from threading import Thread, RLock
import json

DIR = os.path.dirname(os.path.abspath(__file__))

SIGNAL_CONFIG_FILE = os.path.join(DIR, 'signal_config.yaml')
SVL_JMRI_SERVER_HOST = 'http://127.0.0.1:3000'
SECONDS_BETWEEN_POLLS = 1.5

_config_cache = None
_config_mtime = None


def _LoadConfigCached() -> dict:
    global _config_cache, _config_mtime
    mtime = os.path.getmtime(SIGNAL_CONFIG_FILE)
    if _config_cache is None or mtime != _config_mtime:
        _config_cache = signal_config.LoadConfig(SIGNAL_CONFIG_FILE)
        _config_mtime = mtime
        logging.info('Reloaded config from %s', SIGNAL_CONFIG_FILE)
    return _config_cache


# Shared state for the HTTP status endpoint. Populated by Update(),
# read by the status server. Lock is mandatory: Update() runs in main thread,
# HTTP requests come in on the http.server thread.
_status_lock = RLock()
_status_state = {
    'masts': {},        # {mast_name: {type, aspect, appearance, upper, lower, lit, reason, ts}}
    'last_update_ts': None,
    'signaling_mode': None,  # 'block' or 'dispatch'
    'config_path': SIGNAL_CONFIG_FILE,
    'uptime_sec': 0,
    'consecutive_failures': 0,
}
_start_time = time.time()


@dataclass
class LayoutContext:
    turnout_state: dict
    sensor_state: dict
    memory_vars: dict
    masts: dict


def _SignalHeadTree(address, name):
    CLASS = 'jmri.implementation.configurexml.DccSignalHeadXml'
    head = etree.Element('signalhead')
    head.attrib['class'] = CLASS

    system_name_text = f'NH${address}'
    head.attrib['systemName'] = system_name_text
    head.attrib['userName'] = name

    systemName = etree.SubElement(head, 'systemName')
    systemName.text = system_name_text

    userName = etree.SubElement(head, 'userName')
    userName.text = name

    useAddressOffSet = etree.SubElement(head, 'useAddressOffSet')
    useAddressOffSet.text = 'no'

    # Default NCE Light-It Aspect numbers.
    aspects = {
        'Red': 0,
        'Yellow': 1,
        'Green': 2,
        'Flashing Red': 3,
        'Flashing Yellow': 4,
        'Flashing Green': 5,
        # Not implemented.
        'Dark': 31,
        'Lunar': 31,
        'Flashing Lunar': 31,
    }
    for aspect_name, aspect_num in aspects.items():
        aspect = etree.SubElement(head, 'aspect', defines=aspect_name)
        number = etree.SubElement(aspect, 'number')
        number.text = str(aspect_num)

    return head


def OutputXML():
    signal_masts_by_name = signal_config.LoadConfig(SIGNAL_CONFIG_FILE)
    signalheads = etree.Element('signalheads')
    for name, mast in signal_masts_by_name.items():
        if isinstance(mast, signal_config.DoubleHeadTriLightMast):
            signalheads.append(_SignalHeadTree(mast._upper_head_address, f'{mast._mast_name}_upper'))
            signalheads.append(_SignalHeadTree(mast._lower_head_address, f'{mast._mast_name}_lower'))
        elif isinstance(mast, signal_config.SingleHeadTriLightMast):
            signalheads.append(_SignalHeadTree(mast._head_address, mast._mast_name))

    print(etree.tostring(signalheads, pretty_print=True))


def _DetermineMastTypeAndHeads(mast, summary) -> dict:
    """Translate a Mast + SignalSummary into a uniform per-head appearance dict."""
    appearance = summary.appearance or ''
    # appearance is either "X" or "X over Y" (from PrettyAppearance), where X/Y
    # are HEAD_* values with the 'HEAD_' prefix stripped.
    parts = [p.strip() for p in appearance.split(' over ')]

    if isinstance(mast, signal_config.DoubleHeadTriLightMast):
        upper = f'HEAD_{parts[0]}' if parts and parts[0] else 'HEAD_DARK'
        lower = f'HEAD_{parts[1]}' if len(parts) > 1 else 'HEAD_DARK'
        return {
            'type': 'double_tri',
            'upper': upper,
            'lower': lower,
            'lit': _HeadAppearanceToLitColors(upper) + _HeadAppearanceToLitColors(lower, suffix='_lower'),
        }
    if isinstance(mast, signal_config.SingleHeadCPLMast):
        upper = f'HEAD_{parts[0]}' if parts and parts[0] else 'HEAD_DARK'
        return {'type': 'cpl', 'upper': upper, 'lower': None, 'lit': _HeadAppearanceToLitColors(upper)}
    # default: SingleHeadTriLightMast
    upper = f'HEAD_{parts[0]}' if parts and parts[0] else 'HEAD_DARK'
    return {'type': 'single_tri', 'upper': upper, 'lower': None, 'lit': _HeadAppearanceToLitColors(upper)}


def _HeadAppearanceToLitColors(head_appearance: str, suffix: str = '') -> list:
    """Translate a HEAD_* value into a list of {color, flashing, head} dicts."""
    head = 'lower' if suffix == '_lower' else 'upper'
    mapping = {
        'HEAD_GREEN':           ('green', False),
        'HEAD_FLASHING_GREEN':  ('green', True),
        'HEAD_YELLOW':          ('yellow', False),
        'HEAD_FLASHING_YELLOW': ('yellow', True),
        'HEAD_RED':             ('red', False),
        'HEAD_FLASHING_RED':    ('red', True),
        'HEAD_LUNAR':           ('lunar', False),
        'HEAD_DARK':            (None, False),
    }
    color, flashing = mapping.get(head_appearance, (None, False))
    if color is None:
        return []
    return [{'color': color, 'flashing': flashing, 'head': head}]


def _StartStatusServer(port: int) -> None:
    """Start a tiny HTTP server in a daemon thread that exposes /status and /reload."""
    import http.server
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def _json_response(self, code: int, obj: dict) -> None:
            payload = json.dumps(obj).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path in ('/status', '/status/'):
                with _status_lock:
                    payload = json.dumps(_status_state).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                # CORS so the editor (running on a different port) can poll us.
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(payload)
            elif self.path in ('/reload', '/reload/'):
                global _config_cache, _config_mtime
                _config_cache = None
                _config_mtime = None
                logging.info('Config reload forced via HTTP')
                self._json_response(200, {'reloaded': True})
            else:
                self._json_response(404, {'error': 'not found'})

        def log_message(self, fmt, *args):
            logging.debug('Status HTTP: ' + fmt, *args)

    server = http.server.ThreadingHTTPServer(('0.0.0.0', port), Handler)
    t = Thread(target=server.serve_forever, name='svl-status-http', daemon=True)
    t.start()
    logging.info('Signal status HTTP server listening on port %d', port)
    print(f'Signal status HTTP server listening on port {port}')


def Update(jmri_handle, openlcb_handle, reset_terminal: bool = False) -> bool:
    try:
        signal_masts_by_name = _LoadConfigCached()

        context = LayoutContext(
            turnout_state=jmri_handle.GetCurrentTurnoutData(),
            sensor_state=jmri_handle.GetCurrentSensorData(),
            memory_vars=jmri_handle.GetMemoryVariables(),
            masts=signal_masts_by_name,
        )

        table = prettytable.PrettyTable()
        table.field_names = ['Mast', 'Aspect', 'Appearance', 'Reason']

        # Rebuild the snapshot in a local dict, then atomically swap it into
        # _status_state so HTTP readers never see a half-updated state.
        new_masts = {}
        now = time.time()

        for mast_name in sorted(signal_masts_by_name.keys(), key=str.lower):
            mast = signal_masts_by_name[mast_name]
            logging.debug('Configuring signal mast %s', mast)
            layout_handle = openlcb_handle if mast.PostToOpenlcb() else jmri_handle
            summary = mast.PutAspect(context, layout_handle=layout_handle, jmri_for_mem=jmri_handle)
            table.add_row([str(mast), summary.aspect, summary.appearance, summary.reason])

            head_info = _DetermineMastTypeAndHeads(mast, summary)
            new_masts[mast_name] = {
                'type': head_info['type'],
                'aspect': summary.aspect,
                'appearance': summary.appearance,
                'upper': head_info['upper'],
                'lower': head_info['lower'],
                'lit': head_info['lit'],
                'reason': summary.reason,
                'ts': now,
            }

        # Commit per-mast snapshot before dispatch-mode check — that call can raise
        # (e.g. memory var not in JMRI yet), but aspects are already valid and worth publishing.
        signaling_mode = 'block'
        signaling_mode_suffix = ' [Block Signaling]'
        with _status_lock:
            _status_state['masts'] = new_masts
            _status_state['last_update_ts'] = now
            _status_state['signaling_mode'] = signaling_mode

        try:
            if signal_config._DispatchSignalingMode(context):
                signaling_mode_suffix = ' [Dispatch Signaling]'
                signaling_mode = 'dispatch'
                with _status_lock:
                    _status_state['signaling_mode'] = signaling_mode
        except Exception:
            logging.exception('DispatchSignalingMode check failed; assuming block')

        if reset_terminal:
            print(chr(27) + '[2J')
        print('Signal Server Status' + signaling_mode_suffix)
        print(table)
        return True

    except Exception as e:
        logging.exception(e)
        return False


def _SetAllMastsStopped(jmri_handle, openlcb_handle) -> None:
    """Send STOP (red) to every mast. Called on shutdown."""
    try:
        masts = _LoadConfigCached()
    except Exception:
        logging.exception('Could not load config during shutdown')
        return
    for mast_name, mast in masts.items():
        try:
            layout_handle = openlcb_handle if mast.PostToOpenlcb() else jmri_handle
            if isinstance(mast, signal_config.DoubleHeadTriLightMast):
                layout_handle.SetTriLightSignalHeadAppearance(
                    f'{mast_name}_upper', mast._upper_head_address, HEAD_RED)
                layout_handle.SetTriLightSignalHeadAppearance(
                    f'{mast_name}_lower', mast._lower_head_address, HEAD_RED)
            elif isinstance(mast, signal_config.SingleHeadCPLMast):
                layout_handle.SetLampAppearance(mast._red_address, 'ON')
                layout_handle.SetLampAppearance(mast._green_address, 'OFF')
                layout_handle.SetLampAppearance(mast._yellow_address, 'OFF')
                layout_handle.SetLampAppearance(mast._lunar_address, 'OFF')
            else:
                layout_handle.SetTriLightSignalHeadAppearance(
                    mast_name, mast._head_address, HEAD_RED)
        except Exception:
            logging.exception('Failed to stop mast %s during shutdown', mast_name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pretty', action='store_true')
    parser.add_argument('--output_xml', action='store_true')
    parser.add_argument('--verbose', action='store_true',
                        help='Mirror log output to stdout in addition to the log file.')
    parser.add_argument(
        '--jmri_host', default=SVL_JMRI_SERVER_HOST,
        help=f'JMRI JSON server URL (default: {SVL_JMRI_SERVER_HOST})')
    parser.add_argument(
        '--status_port', type=int, default=0,
        help='If non-zero, start an HTTP server on this port exposing /status '
             'with current mast aspects (for the SVL Signal Editor diagram view).')
    args = parser.parse_args()

    log_fmt = '%(asctime)s %(filename)s:%(lineno)d %(message)s'
    file_handler = logging.handlers.TimedRotatingFileHandler(
        'svl_signal_server.log', when='h', interval=1, backupCount=6)
    handlers = [file_handler]
    if args.verbose:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(format=log_fmt, level=logging.DEBUG, handlers=handlers)

    if args.output_xml:
        OutputXML()
        return

    jmri_handle = jmri.JMRI(args.jmri_host)

    print(f'Waiting for JMRI at {args.jmri_host}...')
    while True:
        try:
            jmri_handle.GetCurrentTurnoutData()
            print('JMRI connected.')
            break
        except Exception:
            logging.info('JMRI not ready, retrying in 5s')
            print('JMRI not ready, retrying in 5s...')
            time.sleep(5)

    openlcb_handle = OpenlcbLayoutHandle(None)

    def _shutdown_handler(signum, frame):
        print('\nShutting down — setting all signals to STOP...')
        logging.info('Graceful shutdown requested (signal %d)', signum)
        _SetAllMastsStopped(jmri_handle, openlcb_handle)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    if args.status_port:
        _StartStatusServer(args.status_port)

    _MAX_BACKOFF_SECS = 30
    consecutive_failures = 0

    while True:
        success = Update(jmri_handle, openlcb_handle, reset_terminal=args.pretty)
        if success:
            if consecutive_failures > 0:
                logging.info('JMRI reconnected after %d failure(s)', consecutive_failures)
                print('JMRI reconnected.')
                consecutive_failures = 0
            sleep_secs = SECONDS_BETWEEN_POLLS
        else:
            consecutive_failures += 1
            sleep_secs = min(SECONDS_BETWEEN_POLLS * (2 ** consecutive_failures), _MAX_BACKOFF_SECS)
            logging.info('JMRI unreachable, retrying in %.1fs (failure %d)', sleep_secs, consecutive_failures)
            print(f'JMRI unreachable, retrying in {sleep_secs:.1f}s...')

        with _status_lock:
            _status_state['uptime_sec'] = int(time.time() - _start_time)
            _status_state['consecutive_failures'] = consecutive_failures

        if args.pretty:
            print('Last Update:', time.ctime(time.time()))
        time.sleep(sleep_secs)


if __name__ == '__main__':
    main()
