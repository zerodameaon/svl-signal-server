import unittest
from unittest.mock import MagicMock

from enums import *
from signal_requirements import SensorRequirement, TurnoutRequirement
from signal_config import (
    _GetNextMostPermissiveAspect,
    _DispatchSignalingMode,
    SignalSummary,
    SingleHeadTriLightMast,
    SingleHeadCPLMast,
    DoubleHeadTriLightMast,
    SignalRoute,
)
from signal_server import _HeadAppearanceToLitColors, _DetermineMastTypeAndHeads, LayoutContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx(turnouts=None, sensors=None, memory_vars=None, masts=None):
    return LayoutContext(
        turnout_state=turnouts or {},
        sensor_state=sensors or {},
        memory_vars=memory_vars or {SVL_DISPATCH_SIGNAL_CONTROL_MEMORY_VAR_NAME: 'no'},
        masts=masts or {},
    )


def _next_mast(aspect):
    m = MagicMock()
    m.GetIntendedAspect.return_value = (aspect, 'ok')
    return m


class FakeLayoutHandle:
    def __init__(self):
        self.tri_light_calls = []
        self.lamp_calls = []
        self.mem_calls = []

    def SetTriLightSignalHeadAppearance(self, mast_name, head_address, appearance, ignore_cache=False):
        self.tri_light_calls.append((mast_name, head_address, appearance))

    def SetLampAppearance(self, lamp_first_eventid, appearance, ignore_cache=False):
        self.lamp_calls.append((lamp_first_eventid, appearance))

    def SetMemoryVar(self, var_name, value):
        self.mem_calls.append((var_name, value))


# ---------------------------------------------------------------------------
# ConvertAspectToDivergingAspect
# ---------------------------------------------------------------------------

class TestConvertAspectToDivergingAspect(unittest.TestCase):

    def test_clear(self):
        self.assertEqual(ConvertAspectToDivergingAspect(SIGNAL_CLEAR), SIGNAL_DIVERGING_CLEAR)

    def test_advance_approach(self):
        self.assertEqual(ConvertAspectToDivergingAspect(SIGNAL_ADVANCE_APPROACH), SIGNAL_DIVERGING_ADVANCE_APPROACH)

    def test_approach(self):
        self.assertEqual(ConvertAspectToDivergingAspect(SIGNAL_APPROACH), SIGNAL_DIVERGING_APPROACH)

    def test_restricting(self):
        self.assertEqual(ConvertAspectToDivergingAspect(SIGNAL_RESTRICTING), SIGNAL_DIVERGING_RESTRICTING)

    def test_approach_clear_fifty(self):
        self.assertEqual(ConvertAspectToDivergingAspect(SIGNAL_APPROACH_CLEAR_FIFTY), SIGNAL_DIVERGING_CLEAR_LIMITED)

    def test_approach_clear_sixty(self):
        self.assertEqual(ConvertAspectToDivergingAspect(SIGNAL_APPROACH_CLEAR_SIXTY), SIGNAL_DIVERGING_CLEAR_LIMITED)

    def test_stop_stays_stop(self):
        self.assertEqual(ConvertAspectToDivergingAspect(SIGNAL_STOP), SIGNAL_STOP)

    def test_unknown_maps_to_stop(self):
        self.assertEqual(ConvertAspectToDivergingAspect('SIGNAL_UNKNOWN'), SIGNAL_STOP)


# ---------------------------------------------------------------------------
# _GetNextMostPermissiveAspect
# ---------------------------------------------------------------------------

class TestGetNextMostPermissiveAspect(unittest.TestCase):

    def test_clear_stays_clear(self):
        self.assertEqual(_GetNextMostPermissiveAspect(SIGNAL_CLEAR), SIGNAL_CLEAR)

    def test_advance_approach_becomes_clear(self):
        self.assertEqual(_GetNextMostPermissiveAspect(SIGNAL_ADVANCE_APPROACH), SIGNAL_CLEAR)

    def test_approach_becomes_advance_approach(self):
        self.assertEqual(_GetNextMostPermissiveAspect(SIGNAL_APPROACH), SIGNAL_ADVANCE_APPROACH)

    def test_restricting_becomes_approach(self):
        self.assertEqual(_GetNextMostPermissiveAspect(SIGNAL_RESTRICTING), SIGNAL_APPROACH)

    def test_stop_becomes_approach(self):
        self.assertEqual(_GetNextMostPermissiveAspect(SIGNAL_STOP), SIGNAL_APPROACH)

    def test_dark_becomes_approach(self):
        self.assertEqual(_GetNextMostPermissiveAspect(SIGNAL_DARK), SIGNAL_APPROACH)

    def test_approach_clear_fifty_becomes_clear(self):
        self.assertEqual(_GetNextMostPermissiveAspect(SIGNAL_APPROACH_CLEAR_FIFTY), SIGNAL_CLEAR)

    def test_approach_clear_sixty_becomes_clear(self):
        self.assertEqual(_GetNextMostPermissiveAspect(SIGNAL_APPROACH_CLEAR_SIXTY), SIGNAL_CLEAR)

    def test_diverging_clear_becomes_approach_clear_sixty(self):
        self.assertEqual(_GetNextMostPermissiveAspect(SIGNAL_DIVERGING_CLEAR), SIGNAL_APPROACH_CLEAR_SIXTY)

    def test_diverging_clear_limited_becomes_approach_clear_fifty(self):
        self.assertEqual(_GetNextMostPermissiveAspect(SIGNAL_DIVERGING_CLEAR_LIMITED), SIGNAL_APPROACH_CLEAR_FIFTY)

    def test_diverging_advance_approach_becomes_approach_clear_fifty(self):
        self.assertEqual(_GetNextMostPermissiveAspect(SIGNAL_DIVERGING_ADVANCE_APPROACH), SIGNAL_APPROACH_CLEAR_FIFTY)

    def test_diverging_approach_becomes_approach_diverging(self):
        self.assertEqual(_GetNextMostPermissiveAspect(SIGNAL_DIVERGING_APPROACH), SIGNAL_APPROACH_DIVERGING)

    def test_diverging_restricting_becomes_approach_diverging(self):
        self.assertEqual(_GetNextMostPermissiveAspect(SIGNAL_DIVERGING_RESTRICTING), SIGNAL_APPROACH_DIVERGING)


# ---------------------------------------------------------------------------
# _DispatchSignalingMode
# ---------------------------------------------------------------------------

class TestDispatchSignalingMode(unittest.TestCase):

    def _ctx(self, val):
        return _ctx(memory_vars={SVL_DISPATCH_SIGNAL_CONTROL_MEMORY_VAR_NAME: val})

    def test_yes_returns_true(self):
        self.assertTrue(_DispatchSignalingMode(self._ctx('yes')))

    def test_yes_case_insensitive(self):
        self.assertTrue(_DispatchSignalingMode(self._ctx('YES')))

    def test_no_returns_false(self):
        self.assertFalse(_DispatchSignalingMode(self._ctx('no')))

    def test_missing_var_returns_false(self):
        self.assertFalse(_DispatchSignalingMode(_ctx(memory_vars={})))

    def test_empty_string_returns_false(self):
        self.assertFalse(_DispatchSignalingMode(self._ctx('')))


# ---------------------------------------------------------------------------
# SignalSummary
# ---------------------------------------------------------------------------

class TestSignalSummary(unittest.TestCase):

    def test_strips_signal_prefix(self):
        self.assertEqual(SignalSummary('SIGNAL_CLEAR', 'GREEN', 'r').aspect, 'CLEAR')

    def test_preserves_appearance_and_reason(self):
        s = SignalSummary('SIGNAL_STOP', 'RED', 'some reason')
        self.assertEqual(s.appearance, 'RED')
        self.assertEqual(s.reason, 'some reason')

    def test_pretty_appearance_single(self):
        self.assertEqual(SignalSummary.PrettyAppearance('HEAD_GREEN'), 'GREEN')

    def test_pretty_appearance_double(self):
        self.assertEqual(SignalSummary.PrettyAppearance('HEAD_RED', 'HEAD_GREEN'), 'RED over GREEN')

    def test_pretty_appearance_strips_head_prefix(self):
        self.assertEqual(SignalSummary.PrettyAppearance('HEAD_FLASHING_YELLOW'), 'FLASHING_YELLOW')


# ---------------------------------------------------------------------------
# SensorRequirement
# ---------------------------------------------------------------------------

class TestSensorRequirement(unittest.TestCase):

    def _req(self, required=SENSOR_INACTIVE, permissive=False):
        return SensorRequirement('LS1', required, is_permissive=permissive)

    def test_satisfied_when_matches(self):
        self.assertTrue(self._req().IsSatisfied({}, {'LS1': SENSOR_INACTIVE}))

    def test_not_satisfied_when_differs(self):
        self.assertFalse(self._req().IsSatisfied({}, {'LS1': SENSOR_ACTIVE}))

    def test_satisfied_when_unknown(self):
        self.assertTrue(self._req().IsSatisfied({}, {'LS1': SENSOR_UNKNOWN}))

    def test_not_satisfied_when_missing(self):
        self.assertFalse(self._req().IsSatisfied({}, {}))

    def test_permissive_returns_occupied_permissive(self):
        self.assertEqual(
            self._req(permissive=True).IsSatisfied({}, {'LS1': SENSOR_ACTIVE}),
            'OCCUPIED_PERMISSIVE')

    def test_non_permissive_wrong_state_returns_false(self):
        self.assertFalse(self._req(permissive=False).IsSatisfied({}, {'LS1': SENSOR_ACTIVE}))


# ---------------------------------------------------------------------------
# TurnoutRequirement
# ---------------------------------------------------------------------------

class TestTurnoutRequirement(unittest.TestCase):

    def _req(self, required=TURNOUT_CLOSED):
        return TurnoutRequirement('NT1', required)

    def test_satisfied_when_matches(self):
        self.assertTrue(self._req().IsSatisfied({'NT1': TURNOUT_CLOSED}, {}))

    def test_not_satisfied_when_differs(self):
        self.assertFalse(self._req().IsSatisfied({'NT1': TURNOUT_THROWN}, {}))

    def test_not_satisfied_when_missing(self):
        self.assertFalse(self._req().IsSatisfied({}, {}))

    def test_not_satisfied_when_unknown(self):
        self.assertFalse(self._req().IsSatisfied({'NT1': TURNOUT_UNKNOWN}, {}))


# ---------------------------------------------------------------------------
# SingleHeadTriLightMast.GetAppearance
# ---------------------------------------------------------------------------

class TestSingleHeadGetAppearance(unittest.TestCase):

    def _get(self, aspect):
        return SingleHeadTriLightMast.GetAppearance(aspect)

    def test_clear_is_green(self):
        self.assertEqual(self._get(SIGNAL_CLEAR), HEAD_GREEN)

    def test_advance_approach_is_flashing_yellow(self):
        self.assertEqual(self._get(SIGNAL_ADVANCE_APPROACH), HEAD_FLASHING_YELLOW)

    def test_approach_is_yellow(self):
        self.assertEqual(self._get(SIGNAL_APPROACH), HEAD_YELLOW)

    def test_approach_clear_fifty_is_flashing_green(self):
        self.assertEqual(self._get(SIGNAL_APPROACH_CLEAR_FIFTY), HEAD_FLASHING_GREEN)

    def test_approach_clear_sixty_is_flashing_green(self):
        self.assertEqual(self._get(SIGNAL_APPROACH_CLEAR_SIXTY), HEAD_FLASHING_GREEN)

    def test_approach_diverging_is_yellow(self):
        self.assertEqual(self._get(SIGNAL_APPROACH_DIVERGING), HEAD_YELLOW)

    def test_restricting_is_flashing_red(self):
        self.assertEqual(self._get(SIGNAL_RESTRICTING), HEAD_FLASHING_RED)

    def test_stop_is_red(self):
        self.assertEqual(self._get(SIGNAL_STOP), HEAD_RED)

    def test_dark_is_dark(self):
        self.assertEqual(self._get(SIGNAL_DARK), HEAD_DARK)

    def test_diverging_clear_becomes_flashing_green(self):
        # On a 1-head mast, DIVERGING_CLEAR -> APPROACH_CLEAR_FIFTY -> FLASHING_GREEN
        self.assertEqual(self._get(SIGNAL_DIVERGING_CLEAR), HEAD_FLASHING_GREEN)


# ---------------------------------------------------------------------------
# SingleHeadCPLMast.GetAppearance
# ---------------------------------------------------------------------------

class TestCPLGetAppearance(unittest.TestCase):

    def test_restricting_is_lunar(self):
        self.assertEqual(SingleHeadCPLMast.GetAppearance(SIGNAL_RESTRICTING), HEAD_LUNAR)

    def test_diverging_restricting_is_lunar(self):
        self.assertEqual(SingleHeadCPLMast.GetAppearance(SIGNAL_DIVERGING_RESTRICTING), HEAD_LUNAR)

    def test_clear_is_green(self):
        self.assertEqual(SingleHeadCPLMast.GetAppearance(SIGNAL_CLEAR), HEAD_GREEN)

    def test_stop_is_red(self):
        self.assertEqual(SingleHeadCPLMast.GetAppearance(SIGNAL_STOP), HEAD_RED)

    def test_approach_is_yellow(self):
        self.assertEqual(SingleHeadCPLMast.GetAppearance(SIGNAL_APPROACH), HEAD_YELLOW)


# ---------------------------------------------------------------------------
# DoubleHeadTriLightMast.PutAspect
# ---------------------------------------------------------------------------

class TestDoubleHeadPutAspect(unittest.TestCase):

    def _put(self, next_aspect):
        mast = DoubleHeadTriLightMast('TM', upper_head_address=10, lower_head_address=11)
        route = SignalRoute(next_mast_name='next', route_name='main')
        mast.AddRoute(route)
        ctx = _ctx(masts={'next': _next_mast(next_aspect)})
        handle = FakeLayoutHandle()
        summary = mast.PutAspect(ctx, layout_handle=handle)
        upper = handle.tri_light_calls[0][2]
        lower = handle.tri_light_calls[1][2]
        return summary, upper, lower

    def test_head_names_are_upper_and_lower(self):
        mast = DoubleHeadTriLightMast('TM', 10, 11)
        route = SignalRoute(next_mast_name='next', route_name='main')
        mast.AddRoute(route)
        handle = FakeLayoutHandle()
        mast.PutAspect(_ctx(masts={'next': _next_mast(SIGNAL_STOP)}), layout_handle=handle)
        self.assertEqual(handle.tri_light_calls[0][0], 'TM_upper')
        self.assertEqual(handle.tri_light_calls[1][0], 'TM_lower')

    def test_next_clear_gives_clear(self):
        # _GetNextMostPermissiveAspect(CLEAR) → CLEAR; double head shows GREEN over RED
        summary, upper, lower = self._put(SIGNAL_CLEAR)
        self.assertEqual(summary.aspect, 'CLEAR')
        self.assertEqual(upper, HEAD_GREEN)
        self.assertEqual(lower, HEAD_RED)

    def test_next_stop_gives_approach(self):
        summary, upper, lower = self._put(SIGNAL_STOP)
        self.assertEqual(summary.aspect, 'APPROACH')
        self.assertEqual(upper, HEAD_YELLOW)
        self.assertEqual(lower, HEAD_RED)

    def test_next_diverging_clear_gives_approach_clear_sixty(self):
        summary, upper, lower = self._put(SIGNAL_DIVERGING_CLEAR)
        self.assertEqual(summary.aspect, 'APPROACH_CLEAR_SIXTY')
        self.assertEqual(upper, HEAD_YELLOW)
        self.assertEqual(lower, HEAD_FLASHING_GREEN)

    def test_next_diverging_clear_limited_gives_approach_clear_fifty(self):
        summary, upper, lower = self._put(SIGNAL_DIVERGING_CLEAR_LIMITED)
        self.assertEqual(summary.aspect, 'APPROACH_CLEAR_FIFTY')
        self.assertEqual(upper, HEAD_YELLOW)
        self.assertEqual(lower, HEAD_GREEN)

    def test_next_dark_gives_approach(self):
        summary, upper, lower = self._put(SIGNAL_DARK)
        self.assertEqual(summary.aspect, 'APPROACH')

    def test_next_advance_approach_gives_clear(self):
        summary, upper, lower = self._put(SIGNAL_ADVANCE_APPROACH)
        self.assertEqual(summary.aspect, 'CLEAR')
        self.assertEqual(upper, HEAD_GREEN)
        self.assertEqual(lower, HEAD_RED)

    def test_appearance_string_format(self):
        summary, upper, lower = self._put(SIGNAL_STOP)
        self.assertEqual(summary.appearance, 'YELLOW over RED')


# ---------------------------------------------------------------------------
# SignalRoute.GetAspectOrNone
# ---------------------------------------------------------------------------

class TestSignalRoute(unittest.TestCase):

    def _route(self, next_mast_name='next', requirements=None, is_diverging=False, maximum_speed=None):
        route = SignalRoute(next_mast_name=next_mast_name, route_name='test',
                            is_diverging=is_diverging, maximum_speed=maximum_speed)
        for req in (requirements or []):
            route.AddRequirement(req)
        return route

    def test_unsatisfied_turnout_returns_none(self):
        route = self._route(requirements=[TurnoutRequirement('NT1', TURNOUT_CLOSED)])
        ctx = _ctx(turnouts={'NT1': TURNOUT_THROWN}, masts={'next': _next_mast(SIGNAL_CLEAR)})
        aspect, _ = route.GetAspectOrNone(ctx)
        self.assertIsNone(aspect)

    def test_satisfied_requirements_return_aspect(self):
        route = self._route(requirements=[TurnoutRequirement('NT1', TURNOUT_CLOSED)])
        ctx = _ctx(turnouts={'NT1': TURNOUT_CLOSED}, masts={'next': _next_mast(SIGNAL_STOP)})
        aspect, _ = route.GetAspectOrNone(ctx)
        self.assertEqual(aspect, SIGNAL_APPROACH)

    def test_no_requirements_returns_aspect(self):
        route = self._route()
        ctx = _ctx(masts={'next': _next_mast(SIGNAL_CLEAR)})
        aspect, _ = route.GetAspectOrNone(ctx)
        self.assertEqual(aspect, SIGNAL_CLEAR)

    def test_diverging_route_converts_aspect(self):
        route = self._route(is_diverging=True)
        ctx = _ctx(masts={'next': _next_mast(SIGNAL_CLEAR)})
        aspect, _ = route.GetAspectOrNone(ctx)
        self.assertEqual(aspect, SIGNAL_DIVERGING_CLEAR)

    def test_slow_speed_caps_at_approach(self):
        route = self._route(maximum_speed='slow')
        ctx = _ctx(masts={'next': _next_mast(SIGNAL_CLEAR)})
        aspect, _ = route.GetAspectOrNone(ctx)
        self.assertEqual(aspect, SIGNAL_APPROACH)

    def test_slow_speed_allows_stop(self):
        route = self._route(maximum_speed='slow')
        ctx = _ctx(masts={'next': _next_mast(SIGNAL_STOP)})
        aspect, _ = route.GetAspectOrNone(ctx)
        # next=STOP -> this mast = APPROACH, which slow doesn't cap further
        self.assertEqual(aspect, SIGNAL_APPROACH)

    def test_restricting_speed_caps_at_restricting(self):
        route = self._route(maximum_speed='restricting')
        ctx = _ctx(masts={'next': _next_mast(SIGNAL_CLEAR)})
        aspect, _ = route.GetAspectOrNone(ctx)
        self.assertEqual(aspect, SIGNAL_RESTRICTING)

    def test_permissive_occupied_sensor_gives_restricting(self):
        req = SensorRequirement('LS1', SENSOR_INACTIVE, is_permissive=True)
        route = self._route(requirements=[req])
        ctx = _ctx(sensors={'LS1': SENSOR_ACTIVE}, masts={'next': _next_mast(SIGNAL_CLEAR)})
        aspect, _ = route.GetAspectOrNone(ctx)
        self.assertEqual(aspect, SIGNAL_RESTRICTING)

    def test_no_next_mast_assumes_dark(self):
        route = SignalRoute(next_mast_name=None, route_name='test')
        ctx = _ctx()
        aspect, _ = route.GetAspectOrNone(ctx)
        self.assertEqual(aspect, SIGNAL_APPROACH)

    def test_invalid_next_mast_raises(self):
        route = self._route(next_mast_name='nonexistent')
        with self.assertRaises(AttributeError):
            route.GetAspectOrNone(_ctx(masts={}))

    def test_multiple_satisfied_requirements(self):
        reqs = [
            TurnoutRequirement('NT1', TURNOUT_CLOSED),
            SensorRequirement('LS1', SENSOR_INACTIVE),
        ]
        route = self._route(requirements=reqs)
        ctx = _ctx(
            turnouts={'NT1': TURNOUT_CLOSED},
            sensors={'LS1': SENSOR_INACTIVE},
            masts={'next': _next_mast(SIGNAL_STOP)},
        )
        aspect, _ = route.GetAspectOrNone(ctx)
        self.assertEqual(aspect, SIGNAL_APPROACH)

    def test_second_requirement_fails(self):
        reqs = [
            TurnoutRequirement('NT1', TURNOUT_CLOSED),
            SensorRequirement('LS1', SENSOR_INACTIVE),
        ]
        route = self._route(requirements=reqs)
        ctx = _ctx(
            turnouts={'NT1': TURNOUT_CLOSED},
            sensors={'LS1': SENSOR_ACTIVE},  # fails
            masts={'next': _next_mast(SIGNAL_CLEAR)},
        )
        aspect, _ = route.GetAspectOrNone(ctx)
        self.assertIsNone(aspect)


# ---------------------------------------------------------------------------
# _HeadAppearanceToLitColors
# ---------------------------------------------------------------------------

class TestHeadAppearanceToLitColors(unittest.TestCase):

    def test_green(self):
        self.assertEqual(_HeadAppearanceToLitColors('HEAD_GREEN'),
                         [{'color': 'green', 'flashing': False, 'head': 'upper'}])

    def test_flashing_green(self):
        self.assertEqual(_HeadAppearanceToLitColors('HEAD_FLASHING_GREEN'),
                         [{'color': 'green', 'flashing': True, 'head': 'upper'}])

    def test_red(self):
        self.assertEqual(_HeadAppearanceToLitColors('HEAD_RED'),
                         [{'color': 'red', 'flashing': False, 'head': 'upper'}])

    def test_flashing_red(self):
        result = _HeadAppearanceToLitColors('HEAD_FLASHING_RED')
        self.assertTrue(result[0]['flashing'])
        self.assertEqual(result[0]['color'], 'red')

    def test_yellow(self):
        result = _HeadAppearanceToLitColors('HEAD_YELLOW')
        self.assertEqual(result[0]['color'], 'yellow')
        self.assertFalse(result[0]['flashing'])

    def test_lunar(self):
        result = _HeadAppearanceToLitColors('HEAD_LUNAR')
        self.assertEqual(result[0]['color'], 'lunar')

    def test_dark_returns_empty(self):
        self.assertEqual(_HeadAppearanceToLitColors('HEAD_DARK'), [])

    def test_unknown_returns_empty(self):
        self.assertEqual(_HeadAppearanceToLitColors('HEAD_UNKNOWN'), [])

    def test_lower_suffix_sets_head_field(self):
        result = _HeadAppearanceToLitColors('HEAD_YELLOW', suffix='_lower')
        self.assertEqual(result[0]['head'], 'lower')

    def test_no_suffix_sets_upper(self):
        result = _HeadAppearanceToLitColors('HEAD_YELLOW')
        self.assertEqual(result[0]['head'], 'upper')


# ---------------------------------------------------------------------------
# _DetermineMastTypeAndHeads
# ---------------------------------------------------------------------------

class TestDetermineMastTypeAndHeads(unittest.TestCase):

    def _summary(self, appearance):
        return SignalSummary('SIGNAL_CLEAR', appearance, 'ok')

    def test_single_tri_type(self):
        mast = SingleHeadTriLightMast('M', 100)
        result = _DetermineMastTypeAndHeads(mast, self._summary('GREEN'))
        self.assertEqual(result['type'], 'single_tri')
        self.assertEqual(result['upper'], 'HEAD_GREEN')
        self.assertIsNone(result['lower'])

    def test_double_tri_type_and_heads(self):
        mast = DoubleHeadTriLightMast('M', 10, 11)
        result = _DetermineMastTypeAndHeads(mast, self._summary('RED over GREEN'))
        self.assertEqual(result['type'], 'double_tri')
        self.assertEqual(result['upper'], 'HEAD_RED')
        self.assertEqual(result['lower'], 'HEAD_GREEN')

    def test_cpl_type(self):
        mast = SingleHeadCPLMast('M', 'g', 'y', 'r', 'l')
        result = _DetermineMastTypeAndHeads(mast, self._summary('GREEN'))
        self.assertEqual(result['type'], 'cpl')
        self.assertIsNone(result['lower'])

    def test_empty_appearance_defaults_to_dark(self):
        mast = SingleHeadTriLightMast('M', 100)
        result = _DetermineMastTypeAndHeads(mast, self._summary(''))
        self.assertEqual(result['upper'], 'HEAD_DARK')

    def test_double_head_missing_lower_defaults_to_dark(self):
        mast = DoubleHeadTriLightMast('M', 10, 11)
        result = _DetermineMastTypeAndHeads(mast, self._summary('RED'))
        self.assertEqual(result['upper'], 'HEAD_RED')
        self.assertEqual(result['lower'], 'HEAD_DARK')

    def test_lit_colors_populated(self):
        mast = SingleHeadTriLightMast('M', 100)
        result = _DetermineMastTypeAndHeads(mast, self._summary('GREEN'))
        self.assertEqual(result['lit'], [{'color': 'green', 'flashing': False, 'head': 'upper'}])

    def test_dark_head_gives_empty_lit(self):
        mast = SingleHeadTriLightMast('M', 100)
        result = _DetermineMastTypeAndHeads(mast, self._summary('DARK'))
        self.assertEqual(result['lit'], [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
