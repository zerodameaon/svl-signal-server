import json
import urllib.request
import urllib.parse
import logging

from enums import *
from layout_handle import LayoutHandle

SIGNAL_ENUM_TO_JMRI_ASPECT = {
	SIGNAL_CLEAR: 'Clear',
	SIGNAL_ADVANCE_APPROACH: 'Advance Approach',
	SIGNAL_APPROACH: 'Approach',
	SIGNAL_APPROACH_CLEAR_SIXTY: 'Approach Clear Sixty',
	SIGNAL_APPROACH_CLEAR_FIFTY: 'Approach Clear Fifty',
	SIGNAL_APPROACH_DIVERGING: 'Approach Diverging',
	SIGNAL_APPROACH_RESTRICTING: 'Approach Restricting',
	SIGNAL_RESTRICTING: 'Restricting',
	SIGNAL_DIVERGING_CLEAR: 'Diverging Clear',
	SIGNAL_DIVERGING_CLEAR_LIMITED: 'Diverging Clear Limited',
	SIGNAL_DIVERGING_ADVANCE_APPROACH: 'Diverging Advance Approach',
	SIGNAL_DIVERGING_APPROACH: 'Diverging Approach',
	SIGNAL_DIVERGING_RESTRICTING: 'Restricting (Diverging)',
	SIGNAL_STOP: 'Stop',
}

# Yup: https://github.com/JMRI/JMRI/blob/master/java/src/jmri/SignalHead.java#L56
HEAD_ENUM_TO_JMRI_NUMBER = {
	HEAD_GREEN: 16,
	HEAD_FLASHING_GREEN: 32,
	HEAD_YELLOW: 4,
	HEAD_FLASHING_YELLOW: 8,
	HEAD_RED: 1,
	HEAD_FLASHING_RED: 2,
	HEAD_DARK: 0,
}


class JMRI(LayoutHandle):

	def __init__(self, jmri_server_address: str):
		self._jmri_server_address = jmri_server_address

	def _GetJsonData(self, url_path: str):
		# JMRI JSON docs:
		# http://jmri.sourceforge.net/help/en/html/web/JsonServlet.shtml
		url = urllib.parse.urljoin(self._jmri_server_address, url_path)
		logging.debug('Fetching JMRI JSON data from %s', url)
		with urllib.request.urlopen(url) as f:
			return json.load(f)

	def _PostToJMRI(self, url: str, json_data: str) -> None:
		req = urllib.request.Request(url, json_data.encode(), {'Content-Type': 'application/json'})
		try:
			with urllib.request.urlopen(req) as f:
				response = f.read()
		except Exception as err:
			logging.error('JMRI POST failed: %s [%s]', err, url)
			return
		logging.debug('JMRI POST response: %s', response)

	def GetCurrentTurnoutData(self) -> dict:
		"""Returns {turnout name -> turnout value}."""
		turnout_states = {}
		for turnout in self._GetJsonData('/json/turnouts'):
			name = turnout['data']['name']
			state = turnout['data']['state']
			if state == 2:
				turnout_state = TURNOUT_CLOSED
			elif state == 4:
				turnout_state = TURNOUT_THROWN
			else:
				turnout_state = TURNOUT_UNKNOWN
			turnout_states[name] = turnout_state
		logging.debug('Fetched data for %d turnouts', len(turnout_states))
		return turnout_states

	def GetCurrentSensorData(self) -> dict:
		"""Returns {sensor name -> sensor value}."""
		sensor_states = {}
		for sensor in self._GetJsonData('/json/sensors'):
			name = sensor['data']['name']
			user_name = sensor['data'].get('userName')
			state = sensor['data']['state']
			if state == 2:
				sensor_state = SENSOR_ACTIVE
			elif state == 4:
				sensor_state = SENSOR_INACTIVE
			else:
				logging.debug('Sensor %s (%s) had unknown json state value %s', name, user_name, state)
				sensor_state = SENSOR_UNKNOWN
			sensor_states[name] = sensor_state
			if user_name:
				sensor_states[user_name] = sensor_state
		logging.debug('Fetched data for %d sensors', len(sensor_states))
		return sensor_states

	def GetMemoryVariables(self) -> dict:
		"""Returns {memory_name -> memory_value}."""
		memory_states = {}
		for var in self._GetJsonData('/json/memory'):
			name = var['data']['name']
			val = var['data']['value']
			memory_states[name] = val
		logging.debug('Fetched %d memory values', len(memory_states))
		return memory_states

	def SetTriLightSignalHeadAppearance(self, head_name: str, unused_address, appearance: str, ignore_cache: bool = False) -> None:
		jmri_number = HEAD_ENUM_TO_JMRI_NUMBER.get(appearance, -1)
		if jmri_number == -1:
			raise RuntimeError(f'Appearance {appearance} invalid')
		path = f'/json/signalHead/{head_name}'
		url = urllib.parse.urljoin(self._jmri_server_address, path)
		json_data = json.dumps({
			'type': 'signalHead',
			'data': {'name': head_name, 'state': jmri_number},
		})
		logging.debug('Posting signal head change to %s: %s', url, json_data)
		self._PostToJMRI(url, json_data)

	def SetSignalMastAspect(self, mast_name: str, unused_address, aspect: str) -> None:
		"""Sets mast_name to an aspect. aspect is a SIGNAL_* enum value."""
		json_state = SIGNAL_ENUM_TO_JMRI_ASPECT.get(aspect)
		if not json_state:
			raise RuntimeError('Aspect invalid')
		path = f'/json/signalMast/{mast_name}'
		url = urllib.parse.urljoin(self._jmri_server_address, path)
		json_data = json.dumps({
			'type': 'signalMast',
			'data': {'name': mast_name, 'state': json_state},
		})
		logging.debug('Posting signal aspect change to %s: %s', url, json_data)
		self._PostToJMRI(url, json_data)

	def SetMemoryVar(self, var_name: str, value: str) -> None:
		path = f'/json/memory/{var_name}'
		url = urllib.parse.urljoin(self._jmri_server_address, path)
		json_data = json.dumps({
			'type': 'memory',
			'data': {'value': value},
		})
		logging.info('Posting memory var to %s: %s', url, json_data)
		self._PostToJMRI(url, json_data)
