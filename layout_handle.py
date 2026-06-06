from abc import ABC, abstractmethod


class LayoutHandle(ABC):

    @abstractmethod
    def SetTriLightSignalHeadAppearance(self, mast_name: str, head_address, appearance: str, ignore_cache: bool = False) -> None:
        ...

    def SetLampAppearance(self, lamp_first_eventid: str, appearance: str, ignore_cache: bool = False) -> None:
        raise NotImplementedError(f'{type(self).__name__} does not support SetLampAppearance')
