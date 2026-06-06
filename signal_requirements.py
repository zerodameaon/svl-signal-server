import logging
from enums import *


class Requirement:

    def IsSatisfied(self, turnouts: dict, sensors: dict):
        raise NotImplementedError('Not implemented')


class SensorRequirement(Requirement):

    def __init__(self, sensor_name: str, required_state: str, is_permissive: bool = False):
        self._sensor_name = sensor_name
        self._required_state = required_state
        self._is_permissive = is_permissive

    def __str__(self) -> str:
        return f'{{{self._sensor_name}={self._required_state}}}'

    def IsSatisfied(self, turnouts: dict, sensors: dict):
        actual_sensor_state = sensors.get(self._sensor_name)
        if not actual_sensor_state:
            logging.error('Required sensor %s not found in provided data', self._sensor_name)
            return False
        if actual_sensor_state in [self._required_state, SENSOR_UNKNOWN]:
            logging.debug('%s satisfied', self)
            return True
        if self._is_permissive:
            logging.debug('%s occupied but permissive', self)
            return 'OCCUPIED_PERMISSIVE'
        logging.debug('%s not satisfied (actual state: %s)', self, actual_sensor_state)
        return False


class TurnoutRequirement(Requirement):

    def __init__(self, turnout_name: str, required_state: str):
        self._turnout_name = turnout_name
        self._required_state = required_state

    def __str__(self) -> str:
        return f'{{{self._turnout_name}={self._required_state}}}'

    def IsSatisfied(self, turnouts: dict, sensors: dict) -> bool:
        actual_turnout_state = turnouts.get(self._turnout_name)
        if not actual_turnout_state:
            logging.error('Required turnout %s not found in provided data', self._turnout_name)
            return False
        if actual_turnout_state == self._required_state:
            logging.debug('%s satisfied', self)
            return True
        logging.debug('%s not satisfied (actual state: %s)', self, actual_turnout_state)
        return False
