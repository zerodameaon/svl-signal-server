import logging
from dataclasses import dataclass
from enums import *
from signal_requirements import *
import yaml


def _GetNextMostPermissiveAspect(aspect: str) -> str:
    logging.debug('Computing next most permissive aspect for %s', aspect)
    if 'SIGNAL_APPROACH_CLEAR_' in aspect:
        return SIGNAL_CLEAR
    if 'SIGNAL_APPROACH_' in aspect:
        return SIGNAL_ADVANCE_APPROACH

    if 'SIGNAL_DIVERGING_' in aspect:
        return {
            SIGNAL_DIVERGING_CLEAR: SIGNAL_APPROACH_CLEAR_SIXTY,
            SIGNAL_DIVERGING_CLEAR_LIMITED: SIGNAL_APPROACH_CLEAR_FIFTY,
            SIGNAL_DIVERGING_ADVANCE_APPROACH: SIGNAL_APPROACH_CLEAR_FIFTY,
            SIGNAL_DIVERGING_APPROACH: SIGNAL_APPROACH_DIVERGING,
            SIGNAL_DIVERGING_RESTRICTING: SIGNAL_APPROACH_DIVERGING,
        }.get(aspect, SIGNAL_RESTRICTING)

    # Non-diverging aspects
    return {
        SIGNAL_CLEAR: SIGNAL_CLEAR,
        SIGNAL_ADVANCE_APPROACH: SIGNAL_CLEAR,
        SIGNAL_APPROACH: SIGNAL_ADVANCE_APPROACH,
        SIGNAL_RESTRICTING: SIGNAL_APPROACH,
        SIGNAL_STOP: SIGNAL_APPROACH,
        SIGNAL_DARK: SIGNAL_APPROACH,
    }.get(aspect, SIGNAL_RESTRICTING)


def _DispatchSignalingMode(context) -> bool:
    val = context.memory_vars.get(SVL_DISPATCH_SIGNAL_CONTROL_MEMORY_VAR_NAME)
    return val is not None and val.lower() == 'yes'


def _GetDispatchVarContent(context, var_name: str):
    """Returns ((section_status, dispatcher_label), reason) or (None, reason)."""
    var_value = context.memory_vars.get(var_name)
    if not var_value:
        return None, f'Dispatch var {var_name} not found'
    logging.info('  JMRI has a value for this variable: %s', var_value)
    value_parts = var_value.split(':')
    if len(value_parts) != 3:
        return None, f'Invalid memory content: {var_value}'
    _unk, section_status, dispatcher_label = value_parts
    return (section_status, dispatcher_label), 'OK'


@dataclass
class SignalSummary:
    aspect: str
    appearance: str
    reason: str

    def __post_init__(self):
        self.aspect = self.aspect.replace('SIGNAL_', '')

    @classmethod
    def PrettyAppearance(cls, head1: str, head2: str = None) -> str:
        head1 = head1.replace('HEAD_', '')
        if head2:
            head2 = head2.replace('HEAD_', '')
            return f'{head1} over {head2}'
        return head1


class SignalMast:

    def __init__(self, mast_name: str):
        self._mast_name = mast_name
        self._routes = []

    def AddRoute(self, route) -> None:
        self._routes.append(route)

    def __str__(self) -> str:
        return self._mast_name

    def PostToOpenlcb(self) -> bool:
        return False

    def HasRouteDispatchConfigs(self) -> bool:
        for route in self._routes:
            if route._dispatch_config:
                return True
        return False

    def _SetJMRIMemoryVariable(self, jmri, head_name: str, appearance: str) -> None:
        var_name = f'IMsignalhead_{head_name}'.upper()
        jmri.SetMemoryVar(var_name, appearance.replace('HEAD_', ''))

    def GetIntendedAspect(self, context):
        """Returns a (SIGNAL_*, reason) tuple."""
        logging.debug('  Determining aspect for signal %s', self)

        generated_aspects = []
        no_aspect_reasons = []
        for route in self._routes:
            logging.debug('    Checking route %s', route._route_name)
            aspect_for_route, reason = route.GetAspectOrNone(context)
            logging.debug('    Route %s was %s', route._route_name, aspect_for_route)
            if not aspect_for_route:
                logging.info('Mast %s Route %s has no aspect because %s', self._mast_name, route._route_name, reason)
                no_aspect_reasons.append(f'{route._route_name} has {reason}')
                continue
            generated_aspects.append((aspect_for_route, reason))
        if not generated_aspects:
            logging.debug('  Signal %s has no possible routes', self)
            return SIGNAL_STOP, ',\n'.join(no_aspect_reasons)
        if len(generated_aspects) > 1:
            logging.error('  Signal %s has two non-stop routes (%s)! Using SIGNAL_STOP.', self, generated_aspects)
            return SIGNAL_STOP, 'ERROR: Multiple routes possible'
        return generated_aspects[0]

    def PutAspect(self, context, layout_handle):
        """Enacts the will of this SignalMast."""
        raise NotImplementedError


class SingleHeadCPLMast(SignalMast):

    def __init__(self, mast_name: str, green_addr, yellow_addr, red_addr, lunar_addr):
        super().__init__(mast_name)
        self._green_address = green_addr
        self._yellow_address = yellow_addr
        self._red_address = red_addr
        self._lunar_address = lunar_addr

    def PostToOpenlcb(self) -> bool:
        return True

    @classmethod
    def GetAppearance(cls, aspect: str) -> str:
        if aspect in [SIGNAL_RESTRICTING, SIGNAL_DIVERGING_RESTRICTING]:
            return HEAD_LUNAR
        return SingleHeadTriLightMast.GetAppearance(aspect)

    def PutAspect(self, context, layout_handle, jmri_for_mem=None) -> SignalSummary:
        aspect, reason = self.GetIntendedAspect(context)
        appearance = self.GetAppearance(aspect)
        logging.debug('  %s mapped aspect %s to %s [%s]', self._mast_name, aspect, appearance, reason)

        is_flashing = appearance.startswith('FLASHING')
        color_name_to_addr = {
            'RED': self._red_address,
            'YELLOW': self._yellow_address,
            'GREEN': self._green_address,
            'LUNAR': self._lunar_address,
        }
        for color_name, address in color_name_to_addr.items():
            lamp_state = 'FLASHING' if is_flashing else 'ON' if color_name in appearance else 'OFF'
            layout_handle.SetLampAppearance(address, lamp_state)

        if jmri_for_mem:
            self._SetJMRIMemoryVariable(jmri_for_mem, self._mast_name, appearance)
        return SignalSummary(aspect, SignalSummary.PrettyAppearance(appearance), reason)


class SingleHeadTriLightMast(SignalMast):

    def __init__(self, mast_name: str, head_address):
        super().__init__(mast_name)
        self._head_address = head_address

    def PostToOpenlcb(self) -> bool:
        # Check if head address looks like an event id.
        return len(str(self._head_address)) > 7

    @classmethod
    def GetAppearance(cls, aspect: str) -> str:
        # A 1-head mast on a diverging route is not great.
        if '_DIVERGING_' in aspect:
            old_aspect = aspect
            aspect = aspect.replace('_DIVERGING', '').replace('_CLEAR_LIMITED', '_APPROACH_CLEAR_FIFTY')
            logging.debug('Hackily replaced 1-head diverging aspect %s with simple aspect %s', old_aspect, aspect)
            if aspect == SIGNAL_CLEAR:
                aspect = SIGNAL_APPROACH_CLEAR_FIFTY
        if aspect == SIGNAL_CLEAR:
            return HEAD_GREEN
        elif 'APPROACH_CLEAR' in aspect:
            return HEAD_FLASHING_GREEN
        elif aspect == SIGNAL_ADVANCE_APPROACH:
            return HEAD_FLASHING_YELLOW
        elif 'APPROACH' in aspect:
            return HEAD_YELLOW
        elif aspect == SIGNAL_RESTRICTING:
            return HEAD_FLASHING_RED
        elif aspect == SIGNAL_DARK:
            return HEAD_DARK
        return HEAD_RED

    def PutAspect(self, context, layout_handle, jmri_for_mem=None) -> SignalSummary:
        aspect, reason = self.GetIntendedAspect(context)
        appearance = self.GetAppearance(aspect)
        logging.debug('  %s mapped aspect %s to %s [%s]', self._mast_name, aspect, appearance, reason)
        layout_handle.SetTriLightSignalHeadAppearance(self._mast_name, self._head_address, appearance)
        if jmri_for_mem:
            self._SetJMRIMemoryVariable(jmri_for_mem, self._mast_name, appearance)
        return SignalSummary(aspect, SignalSummary.PrettyAppearance(appearance), reason)


class DoubleHeadTriLightMast(SignalMast):

    def __init__(self, mast_name: str, upper_head_address, lower_head_address):
        super().__init__(mast_name)
        self._upper_head_address = upper_head_address
        self._lower_head_address = lower_head_address

    def PostToOpenlcb(self) -> bool:
        upper_head_looks_eventy = len(str(self._upper_head_address)) > 7
        lower_head_looks_eventy = len(str(self._lower_head_address)) > 7
        if upper_head_looks_eventy != lower_head_looks_eventy:
            raise AttributeError(f'In {self._mast_name}: upper head and lower head must neither or both be event IDs')
        return upper_head_looks_eventy

    def PutAspect(self, context, layout_handle, jmri_for_mem=None) -> SignalSummary:
        aspect, reason = self.GetIntendedAspect(context)
        upper_appearance = lower_appearance = HEAD_RED
        if 'DIVERGING' in aspect:
            if aspect == SIGNAL_APPROACH_DIVERGING:
                upper_appearance = lower_appearance = HEAD_YELLOW
            else:
                # all other *DIVERGING* aspects are red over <something>
                lower_appearance = {
                    SIGNAL_DIVERGING_CLEAR: HEAD_GREEN,
                    SIGNAL_DIVERGING_CLEAR_LIMITED: HEAD_FLASHING_GREEN,
                    SIGNAL_DIVERGING_ADVANCE_APPROACH: HEAD_FLASHING_YELLOW,
                    SIGNAL_DIVERGING_APPROACH: HEAD_YELLOW,
                    SIGNAL_DIVERGING_RESTRICTING: HEAD_FLASHING_RED,
                }.get(aspect, HEAD_RED)
        elif aspect == SIGNAL_APPROACH_CLEAR_SIXTY:
            upper_appearance = HEAD_YELLOW
            lower_appearance = HEAD_FLASHING_GREEN
        elif aspect == SIGNAL_APPROACH_CLEAR_FIFTY:
            upper_appearance = HEAD_YELLOW
            lower_appearance = HEAD_GREEN
        else:
            # Same as normal signal mast, but over red (except dark).
            upper_appearance = SingleHeadTriLightMast.GetAppearance(aspect)
            if upper_appearance == HEAD_DARK:
                lower_appearance = HEAD_DARK

        logging.debug('  Mast %s is %s (%s over %s): %s',
                      self._mast_name, aspect, upper_appearance, lower_appearance, reason)
        upper_head_name = f'{self._mast_name}_upper'
        lower_head_name = f'{self._mast_name}_lower'
        layout_handle.SetTriLightSignalHeadAppearance(upper_head_name, self._upper_head_address, upper_appearance)
        layout_handle.SetTriLightSignalHeadAppearance(lower_head_name, self._lower_head_address, lower_appearance)
        if jmri_for_mem:
            self._SetJMRIMemoryVariable(jmri_for_mem, upper_head_name, upper_appearance)
            self._SetJMRIMemoryVariable(jmri_for_mem, lower_head_name, lower_appearance)
        return SignalSummary(aspect, SignalSummary.PrettyAppearance(upper_appearance, lower_appearance), reason)


class SignalRoute:

    def __init__(self, next_mast_name=None, is_diverging=False, route_name=None,
                 maximum_speed=None, dispatch_config=None):
        self._requirements = []
        self._is_diverging = is_diverging
        self._next_mast_name = next_mast_name
        self._route_name = route_name
        self._maximum_speed = maximum_speed
        self._dispatch_config = dispatch_config

    def AddRequirement(self, requirement) -> None:
        self._requirements.append(requirement)

    def GetAspectOrNone(self, context):
        prefix = f'[{self._route_name}] ' if self._route_name else ''

        route_occupied_permissive = False
        for req in self._requirements:
            satisfied = req.IsSatisfied(context.turnout_state, context.sensor_state)
            if not satisfied:
                return None, f'Unsatisfied: {req}'
            if isinstance(req, SensorRequirement) and satisfied == 'OCCUPIED_PERMISSIVE':
                route_occupied_permissive = True
        logging.debug('  All Requirements satisfied')

        if not self._next_mast_name:
            next_mast_aspect = SIGNAL_DARK
            logging.warning('No next mast configured; assuming dark')
        else:
            next_mast = context.masts.get(self._next_mast_name)
            if not next_mast:
                raise AttributeError(f'Route {self._route_name} has invalid next mast {self._next_mast_name}')
            next_mast_aspect, _ = next_mast.GetIntendedAspect(context)
            logging.debug('Next mast is %s, which is %s', self._next_mast_name, next_mast_aspect)

        next_mast_aspect_pretty = next_mast_aspect.replace('SIGNAL_', '')
        aspect = _GetNextMostPermissiveAspect(next_mast_aspect)
        reason = f'OK to {self._next_mast_name}, which is {next_mast_aspect_pretty}'

        if self._maximum_speed == 'slow':
            if aspect not in [SIGNAL_STOP, SIGNAL_DARK, SIGNAL_RESTRICTING]:
                aspect = SIGNAL_APPROACH
                reason = '[slow-approach] ' + reason
        elif self._maximum_speed == 'restricting':
            if aspect not in [SIGNAL_STOP, SIGNAL_DARK]:
                aspect = SIGNAL_RESTRICTING
                reason = '[max-restricting] ' + reason

        if route_occupied_permissive:
            aspect = SIGNAL_RESTRICTING
            reason = '[occupied-permissive] ' + reason

        # Lack of dispatch clearance should "obscure" this mast.
        if _DispatchSignalingMode(context) and not self._dispatch_config:
            return SIGNAL_DARK, prefix + 'No dispatch config'
        elif _DispatchSignalingMode(context) and self._dispatch_config.ignore:
            prefix += '[Ignoring Dispatch] '
        elif _DispatchSignalingMode(context):
            logging.debug('  Can be configured by dispatch var %s when direction is "%s"',
                          self._dispatch_config.memory_var_name, self._dispatch_config.direction)
            dispatch_section_status, invalid_dispatch_reason = _GetDispatchVarContent(
                context, self._dispatch_config.memory_var_name)
            if not dispatch_section_status:
                return SIGNAL_DARK, invalid_dispatch_reason
            section_status, dispatcher_label = dispatch_section_status
            direction = self._dispatch_config.direction
            if section_status == f'Authorized {direction}':
                # TODO: Figure out if clearance is ending by looking at forward signals
                logging.info('Route %s authorized %s for %s; computing aspect',
                             self._route_name, direction, dispatcher_label)
                prefix = f'[{self._route_name} Authorized {direction} for {dispatcher_label}] '
            elif section_status == f'Occupied {direction}':
                return SIGNAL_RESTRICTING, f'{dispatcher_label} occupied {direction}'
            else:
                return SIGNAL_STOP, f'No dispatch clearance: {section_status}'

        if self._is_diverging:
            div_aspect = ConvertAspectToDivergingAspect(aspect)
            logging.debug('%s: converted aspect %s to diverging aspect %s', self._route_name, aspect, div_aspect)
            return div_aspect, prefix + 'Diverging ' + reason
        return aspect, prefix + reason


class DispatchConfig:

    def __init__(self, memory_var_name: str, direction: str, ignore: bool = False):
        self.memory_var_name = memory_var_name
        self.direction = direction
        self.ignore = ignore


def ParseRoute(route_name: str, route_config: dict) -> SignalRoute:
    """Parse a route config stanza into a SignalRoute."""
    logging.debug('Parsing route %s: %s', route_name, route_config)
    is_diverging = route_config.get('is_diverging', False)
    maximum_speed = route_config.get('maximum_speed')
    if maximum_speed:
        allowed = ['slow', 'restricting']
        if maximum_speed not in allowed:
            raise AttributeError(f'{route_name} has invalid maximum_speed; must be one of {allowed}')

    dispatch_config = None
    dispatch_config_items = route_config.get('dispatch_control')
    if dispatch_config_items:
        if dispatch_config_items.get('ignore'):
            dispatch_config = DispatchConfig('NO_MEMORY_VAR', 'NO_DIRECTION', ignore=True)
        else:
            dispatch_config = DispatchConfig(dispatch_config_items['memory_var'],
                                             dispatch_config_items['direction'])

    route = SignalRoute(route_config.get('next_signal'), is_diverging,
                        route_name=route_name, maximum_speed=maximum_speed,
                        dispatch_config=dispatch_config)
    if 'requirements' not in route_config:
        raise AttributeError('Route config must have requirements')

    for requirement in route_config['requirements']:
        if 'turnout' in requirement:
            turnout_name = requirement['turnout']
            raw_state = requirement['state'].lower()
            state = {'thrown': TURNOUT_THROWN, 'closed': TURNOUT_CLOSED}.get(raw_state)
            if not state:
                raise AttributeError(f'Invalid turnout state {raw_state}')
            logging.debug('  Parsed turnout requirement %s %s', turnout_name, state)
            route.AddRequirement(TurnoutRequirement(turnout_name, state))
        elif 'sensor' in requirement:
            sensor_name = requirement['sensor']
            raw_state = requirement.get('state', False)
            state = {True: SENSOR_ACTIVE, False: SENSOR_INACTIVE}.get(raw_state)
            if not state:
                raise AttributeError(f'Invalid sensor state {raw_state}')
            is_permissive = requirement.get('permissive', False)
            logging.debug('  Parsed sensor requirement %s %s (permissive=%s)', sensor_name, state, is_permissive)
            route.AddRequirement(SensorRequirement(sensor_name, state, is_permissive=is_permissive))
        else:
            raise AttributeError('Signal requirement must have turnout or sensor')
    return route


def LoadConfig(config_file_path: str) -> dict:
    """Returns {signal_name -> SignalMast}."""
    with open(config_file_path, 'r') as f:
        config_data = yaml.load(f, Loader=yaml.Loader)

    signal_entries = {}
    for mast_name, configuration in config_data.items():
        logging.debug('Parsing requirements for mast %s', mast_name)

        if 'head_address' in configuration:
            head = configuration.pop('head_address')
            logging.debug('  Mast %s configured with single head %s', mast_name, head)
            signal = SingleHeadTriLightMast(mast_name, head)
        elif 'upper_head_address' in configuration:
            if 'lower_head_address' not in configuration:
                raise AttributeError('lower_head_address required if upper_head_address provided')
            upper = configuration.pop('upper_head_address')
            lower = configuration.pop('lower_head_address')
            logging.debug('  Mast %s configured with heads %s + %s', mast_name, upper, lower)
            signal = DoubleHeadTriLightMast(mast_name, upper, lower)
        elif 'green_lamp_first_eventid' in configuration:
            for lamp_color in ('green', 'yellow', 'red', 'lunar'):
                config_item_name = f'{lamp_color}_lamp_first_eventid'
                if config_item_name not in configuration:
                    raise AttributeError(f'CPL mast must define {config_item_name}')
            signal = SingleHeadCPLMast(
                mast_name,
                green_addr=configuration.pop('green_lamp_first_eventid'),
                yellow_addr=configuration.pop('yellow_lamp_first_eventid'),
                red_addr=configuration.pop('red_lamp_first_eventid'),
                lunar_addr=configuration.pop('lunar_lamp_first_eventid'),
            )
        else:
            raise AttributeError('Signal must define head_address, {upper,lower}_head_address, or {color}_lamp_first_eventid')

        if 'routes' not in configuration:
            raise AttributeError(f'Signal mast {mast_name} is missing a "routes" stanza')
        for route_name, route_info in configuration.pop('routes').items():
            signal.AddRoute(ParseRoute(route_name, route_info))

        if configuration:
            raise AttributeError(f'Unknown configuration items: {list(configuration.keys())}')

        signal_entries[mast_name] = signal
    return signal_entries
