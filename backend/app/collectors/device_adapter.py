from dataclasses import dataclass
from abc import ABC, abstractmethod

@dataclass
class CommandResult:
    stdout: str
    stderr: str = ''
    exit_status: int = 0

class DeviceAdapter(ABC):
    @abstractmethod
    async def connect(self): ...
    @abstractmethod
    async def disconnect(self): ...
    @abstractmethod
    async def execute_shell(self, command:str, timeout:float|None=None) -> CommandResult: ...
    @abstractmethod
    async def execute_cli(self, command:str, timeout:float|None=None) -> CommandResult: ...
