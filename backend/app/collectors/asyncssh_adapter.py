import asyncio
import asyncssh
from app.collectors.device_adapter import DeviceAdapter, CommandResult
from app.core.config import settings
from app.collectors.prompt_reader import read_until_prompt, PromptTimeout, PromptSessionClosed


class DeviceConnectionError(RuntimeError):
    pass


class DeviceCommandError(RuntimeError):
    pass


class AsyncSSHDeviceAdapter(DeviceAdapter):
    """SSH adapter with a persistent AIM PTY session.

    EC-02 requires a PTY/prompt state machine instead of spawning a new `aim` process per
    command.  Phase D1 implements the root AIM session only.  Sub-mode prompt mappings remain
    part of the reserved platform contract and are intentionally not guessed here.
    """

    def __init__(self, *, ip:str, port:int, username:str, password:str, aim_prompt:str|None=None, aim_executable:str='aim', kex_algs: list[str]|None=None):
        self.ip=ip
        self.port=port
        self.username=username
        self.password=password
        self.aim_prompt=aim_prompt or settings.aim_prompt
        self.aim_executable=aim_executable
        # EC-02 APF1250 runs a legacy BusyBox dropbear that requires
        # diffie-hellman-group14-sha1. asyncssh 2.21 disables it by default.
        self.kex_algs=kex_algs if kex_algs is not None else ['diffie-hellman-group14-sha1']
        self.conn=None
        self._aim_process=None
        self._aim_lock=asyncio.Lock()

    async def connect(self):
        try:
            options=asyncssh.SSHClientConnectionOptions(
                username=self.username,
                password=self.password,
                known_hosts=None,
                kex_algs=self.kex_algs,
            )
            self.conn=await asyncio.wait_for(
                asyncssh.connect(
                    self.ip,
                    port=self.port,
                    options=options,
                ),
                timeout=settings.ssh_connect_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise DeviceConnectionError('SSH_CONNECT_TIMEOUT') from exc
        except asyncssh.PermissionDenied as exc:
            raise DeviceConnectionError('SSH_AUTH_FAILED') from exc
        except Exception as exc:
            raise DeviceConnectionError(f'SSH_CONNECT_FAILED:{type(exc).__name__}') from exc

    async def _close_aim_session(self):
        process=self._aim_process
        self._aim_process=None
        if not process:
            return
        try:
            process.stdin.write('exit\n')
        except Exception:
            pass
        try:
            process.stdin.write_eof()
        except Exception:
            pass
        try:
            await asyncio.wait_for(process.wait_closed(), timeout=1.0)
        except Exception:
            try:
                process.close()
            except Exception:
                pass

    async def disconnect(self):
        await self._close_aim_session()
        if self.conn:
            self.conn.close()
            await self.conn.wait_closed()
            self.conn=None

    async def execute_shell(self, command:str, timeout:float|None=None) -> CommandResult:
        if not self.conn:
            raise DeviceConnectionError('SSH_NOT_CONNECTED')
        try:
            res=await asyncio.wait_for(
                self.conn.run(command, check=False),
                timeout=timeout or settings.ssh_command_timeout,
            )
            return CommandResult(
                stdout=res.stdout or '',
                stderr=res.stderr or '',
                exit_status=int(res.exit_status or 0),
            )
        except asyncio.TimeoutError as exc:
            raise DeviceCommandError('SSH_COMMAND_TIMEOUT') from exc

    async def _ensure_aim_session(self, timeout:float, retries:int=3):
        """Ensure the persistent AIM PTY session is open, retrying the initial
        spawn when the DUT's AIM prompt is not immediately available.

        On the EC-02/APF3260-M the very first `aim` spawn can transiently close the
        prompt (PromptSessionClosed) right after connect; a short retry recovers it.
        Once open, the session is reused (execute_cli/read_aim_chunk/write_aim all
        go through here).
        """
        if not self.conn:
            raise DeviceConnectionError('SSH_NOT_CONNECTED')
        if self._aim_process is not None:
            return self._aim_process
        last: Exception | None = None
        for attempt in range(max(1, retries)):
            try:
                process=await self.conn.create_process(self.aim_executable, term_type='xterm')
                await read_until_prompt(process.stdout, self.aim_prompt, timeout)
                self._aim_process=process
                return process
            except (PromptTimeout, PromptSessionClosed) as exc:
                last=exc
                await asyncio.sleep(2.0 * (attempt + 1))
            except Exception as exc:
                raise DeviceCommandError(f'AIM_SESSION_OPEN_FAILED:{type(exc).__name__}') from exc
        raise DeviceCommandError(f'AIM_SESSION_OPEN_FAILED:{type(last).__name__}') from last

    async def execute_cli(self, command:str, timeout:float|None=None) -> CommandResult:
        if not self.conn:
            raise DeviceConnectionError('SSH_NOT_CONNECTED')
        timeout=timeout or settings.ssh_command_timeout
        async with self._aim_lock:
            process=await self._ensure_aim_session(timeout)
            try:
                process.stdin.write(command+'\n')
                output=await read_until_prompt(process.stdout, self.aim_prompt, timeout)
                clean=output.rsplit(self.aim_prompt,1)[0]
                return CommandResult(stdout=clean, stderr='', exit_status=0)
            except (PromptTimeout, PromptSessionClosed) as exc:
                # The prompt contract is no longer trustworthy.  Drop the PTY so the next action
                # starts from a known root state instead of continuing in an unknown sub-mode.
                await self._close_aim_session()
                raise DeviceCommandError(f'AIM_COMMAND_PROMPT_FAILED:{type(exc).__name__}') from exc
            except Exception as exc:
                await self._close_aim_session()
                raise DeviceCommandError(f'AIM_COMMAND_FAILED:{type(exc).__name__}') from exc

    async def read_aim_chunk(self, timeout: float = 1.0) -> str:
        """Read one raw chunk from the persistent AIM PTY stdout.

        Unlike ``execute_cli`` this does not wait for a prompt; it returns whatever
        AIM has emitted ('' on timeout). Used by the FXS event reader that runs on
        the same event loop that owns the asyncssh connection.
        """
        if not self.conn:
            raise DeviceConnectionError('SSH_NOT_CONNECTED')
        process = await self._ensure_aim_session(10)
        try:
            return await asyncio.wait_for(process.stdout.read(4096), timeout)
        except asyncio.TimeoutError:
            return ''

    async def write_aim(self, command: str) -> None:
        """Write a line to the persistent AIM PTY stdin without reading a prompt."""
        if not self.conn:
            raise DeviceConnectionError('SSH_NOT_CONNECTED')
        process = await self._ensure_aim_session(10)
        process.stdin.write(command + '\n')
