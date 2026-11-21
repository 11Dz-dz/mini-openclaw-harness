import asyncio
import sys

import pytest

from miniclaw.sandbox.output import communicate_bounded
from miniclaw.sandbox.types import SandboxResourceExceeded


async def test_output_limit_terminates_fixture_process_before_it_finishes():
    process = await asyncio.create_subprocess_exec(sys.executable, '-c',
        'import os,time\nfor _ in range(100):\n os.write(1,b"x"*512);time.sleep(.01)',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    with pytest.raises(SandboxResourceExceeded):
        await communicate_bounded(process, 1024, 2)
    assert process.returncode is not None


async def test_timeout_reaps_the_fixture_process():
    process = await asyncio.create_subprocess_exec(sys.executable, '-c',
        'import time;time.sleep(5)', stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    with pytest.raises(TimeoutError):
        await communicate_bounded(process, 1024, .05)
    assert process.returncode is not None
