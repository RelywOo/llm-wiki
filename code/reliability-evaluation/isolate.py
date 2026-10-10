"""Run a benchmark process under inherited, unprivileged Landlock restrictions."""
import argparse
import ctypes as C
import os
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--workspace', required=True)
p.add_argument('--profile', required=True)
p.add_argument('--port', type=int)
p.add_argument('--readonly', action='store_true')
p.add_argument('command', nargs=argparse.REMAINDER)
a = p.parse_args()
libc = C.CDLL(None, use_errno=True)
def checked(value):
    if value < 0:
        raise OSError(C.get_errno(), os.strerror(C.get_errno()))
    return value

abi = checked(libc.syscall(444, 0, 0, 1))
if abi < 6:
    raise SystemExit('Landlock ABI 6 or newer is required; refusing to run unisolated.')
class Rules(C.Structure):
    _fields_ = [('fs', C.c_uint64), ('net', C.c_uint64), ('scoped', C.c_uint64)]
class PathRule(C.Structure):
    _pack_ = 1
    _fields_ = [('access', C.c_uint64), ('fd', C.c_int32)]
class PortRule(C.Structure):
    _fields_ = [('access', C.c_uint64), ('port', C.c_uint64)]
FS = (1 << 16) - 1
READ = 1 | 4 | 8
rules = Rules(FS, 3, 3)
fd = checked(libc.syscall(444, C.byref(rules), C.sizeof(rules), 0))
def allow(path, access):
    path = Path(path)
    if not path.exists():
        return
    if not path.is_dir():
        access &= (1 | 2 | 4 | (1 << 14) | (1 << 15))
    item = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        rule = PathRule(access, item)
        checked(libc.syscall(445, fd, 1, C.byref(rule), 0))
    finally:
        os.close(item)

for path in ['/usr', '/bin', '/lib', '/lib64', '/etc', '/proc', '/sys',
             '/run/systemd/resolve',
             str(Path.home()/'.local/lib/node_modules/@openai/codex'),
             str(Path.home()/'.local/bin/codex')]:
    allow(path, READ)
allow('/dev', FS)
allow(a.workspace, READ if a.readonly else FS)
if a.readonly:
    allow(Path(a.workspace)/'code', FS)
    allow(Path(a.workspace)/'deliverables', FS)
allow(a.profile, FS)
# Only the normal CLI auth file and public model catalog are readable from its home.
# Tokens are never copied; a symlink in the isolated profile points to this file.
allow(Path.home()/'.codex/auth.json', 4)
allow(Path.home()/'.codex/models_cache.json', 4)
for port in [443, 53] + ([a.port] if a.port else []):
    rule = PortRule(2, port)
    checked(libc.syscall(445, fd, 2, C.byref(rule), 0))
checked(libc.prctl(38, 1, 0, 0, 0))
checked(libc.syscall(446, fd, 0))
os.close(fd)
command = a.command[1:] if a.command[:1] == ['--'] else a.command
os.execvpe(command[0], command, os.environ)
