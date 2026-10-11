#!/usr/bin/env python3
"""Run a whole inspection process tree under unprivileged Linux write restrictions."""
from __future__ import annotations

import argparse
import ctypes as C
import os
from pathlib import Path
import platform
import subprocess
import stat
import fcntl
import sys

FS=(1<<16)-1
READ=1|4|8


def checked(value):
    if value<0:raise OSError(C.get_errno(),os.strerror(C.get_errno()))
    return value


def protect(root, output, writable):
    root=root.resolve(strict=True)
    if not (root/'.memory').is_dir():raise ValueError('installed .memory directory required')
    env=dict(os.environ,GIT_OPTIONAL_LOCKS='0')
    protected=[root]
    # Protect external targets already reachable through project symlinks too.
    protected.extend(entry.resolve() for entry in root.rglob('*') if entry.is_symlink())
    for option in ('--absolute-git-dir','--git-common-dir'):
        result=subprocess.run(['git','-C',str(root),'rev-parse',option],env=env,text=True,capture_output=True)
        if result.returncode:raise ValueError('root must be a Git working tree')
        protected.append((root/result.stdout.strip()).resolve())
    allowed=[]
    for value in [output,*writable]:
        path=value.absolute();cursor=path
        while cursor!=cursor.parent:
            if cursor.is_symlink():raise ValueError('writable path symlinks are not supported')
            cursor=cursor.parent
        path=path.resolve()
        if any(path.is_relative_to(p) or p.is_relative_to(path) for p in protected):
            raise ValueError('writable paths must be disjoint from the project and Git storage')
        if path.exists() and not path.is_dir():raise ValueError('writable path must be a directory')
        if path.exists():
            for entry in path.rglob('*'):
                if not entry.is_symlink() and entry.is_file() and entry.stat().st_nlink>1:
                    raise ValueError('writable directories cannot contain existing hard links')
        allowed.append(path)
    for fd in range(3):
        info=os.fstat(fd)
        if stat.S_ISSOCK(info.st_mode):raise ValueError('socket standard streams are not supported')
        if stat.S_ISREG(info.st_mode) and fcntl.fcntl(fd,fcntl.F_GETFL)&os.O_ACCMODE!=os.O_RDONLY:
            target=Path(os.readlink(f'/proc/self/fd/{fd}')).resolve()
            if info.st_nlink>1 or any(target.is_relative_to(p) for p in protected):
                raise ValueError('writable standard streams cannot target protected files or hard links')
    return root,allowed


def restrict_syscalls(libc, network):
    """Block external UNIX service delegation and alternate socket creation paths.

    Classic seccomp BPF reads seccomp_data: syscall nr at 0, arch at 4, args at 16.
    Architecture checks prevent compatibility syscall entry points bypassing the filter.
    """
    machine=platform.machine()
    if machine=='x86_64':
        arch,socket_call,ioctl,ptrace,vm_write=0xC000003E,41,16,101,311
        metadata=(90,91,92,93,94,132,188,189,190,197,198,199,235,260,261,268,280,452)
        sysv_ipc=(29,30,31,64,65,66,67,68,69,70,71,220)
    else:
        arch,socket_call,ioctl,ptrace,vm_write=0xC00000B7,198,29,117,271
        metadata=(5,6,7,14,15,16,52,53,54,55,88,452)
        sysv_ipc=tuple(range(186,198))
    allow,deny,kill=0x7fff0000,0x50000|1,0x80000000
    ops=[(0x20,0,0,4),(0x15,1,0,arch),(0x06,0,0,kill),
         (0x20,0,0,0),(0x45,0,1,0x40000000),(0x06,0,0,deny),
         # Refuse newer syscall families until their write semantics are reviewed.
         (0x35,0,1,453),(0x06,0,0,deny),
         (0x15,0,7,ioctl),(0x20,0,0,24),(0x45,0,1,0x40000000),
         (0x06,0,0,deny),(0x15,1,0,0x5412),
         (0x15,0,1,0x541c),(0x06,0,0,deny),(0x20,0,0,0)]
    # Landlock does not mediate metadata mutation. Deny these calls globally,
    # including scratch directories; classic BPF cannot dereference path arguments.
    for number in (ptrace,vm_write,425,*metadata,*sysv_ipc):
        ops += [(0x15,0,1,number),(0x06,0,0,deny)]
    ops += [(0x15,1,0,socket_call),(0x06,0,0,allow)]
    if network:
        ops += [(0x20,0,0,16),(0x15,2,0,2),(0x15,1,0,10),
                (0x06,0,0,deny),(0x06,0,0,allow)]
    else:ops += [(0x06,0,0,deny)]
    class Filter(C.Structure):
        _fields_=[('code',C.c_ushort),('jt',C.c_ubyte),('jf',C.c_ubyte),('k',C.c_uint32)]
    class Program(C.Structure):
        _fields_=[('length',C.c_ushort),('filter',C.POINTER(Filter))]
    filters=(Filter*len(ops))(*(Filter(*op) for op in ops))
    program=Program(len(ops),filters)
    checked(libc.prctl(22,2,C.byref(program),0,0))


def restrict(allowed, network=False):
    if sys.platform!='linux' or platform.machine() not in {'x86_64','aarch64'}:
        raise ValueError('read-only enforcement requires Linux x86_64/aarch64; refusing unprotected execution')
    libc=C.CDLL(None,use_errno=True)
    abi=checked(libc.syscall(444,0,0,1))
    if abi<6:raise ValueError('Landlock ABI 6+ required; refusing unprotected execution')
    class Rules(C.Structure):
        _fields_=[('fs',C.c_uint64),('net',C.c_uint64),('scoped',C.c_uint64)]
    class PathRule(C.Structure):
        _pack_=1
        _fields_=[('access',C.c_uint64),('fd',C.c_int32)]
    rules=Rules(FS,0,3)
    fd=checked(libc.syscall(444,C.byref(rules),C.sizeof(rules),0))
    def allow(path,access):
        item=os.open(path,os.O_PATH|os.O_CLOEXEC)
        try:
            rule=PathRule(access,item);checked(libc.syscall(445,fd,1,C.byref(rule),0))
        finally:os.close(item)
    try:
        allow('/',READ)
        for directory in allowed:allow(directory,FS)
        allow('/dev/null',2|4|(1<<14))
        checked(libc.prctl(38,1,0,0,0))
        checked(libc.syscall(446,fd,0))
    finally:os.close(fd)
    restrict_syscalls(libc,network)
    return abi


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path.cwd())
    parser.add_argument('--output',type=Path,required=True,help='External writable report/scratch directory.')
    parser.add_argument('--writable',type=Path,action='append',default=[],help='External runtime profile; never inside the project or Git storage.')
    parser.add_argument('--network',action='store_true',help='Opt in to Internet sockets for a model API; external UNIX service connections stay blocked.')
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args()
    command=args.command[1:] if args.command[:1]==['--'] else args.command
    if not command:parser.error('a child command is required after --')
    try:
        root,allowed=protect(args.root,args.output,args.writable)
        for directory in allowed:directory.mkdir(parents=True,exist_ok=True)
        tmp=allowed[0]/'.tmp';tmp.mkdir(exist_ok=True)
        abi=restrict(allowed,args.network)
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',GIT_OPTIONAL_LOCKS='0',
                 TMPDIR=str(tmp),LLM_WIKI_SESSION='read-only',LLM_WIKI_OUTPUT=str(allowed[0]))
        # Landlock does not revoke writable descriptors opened before activation.
        # Preserve only checked stdio, never arbitrary inherited files or sockets.
        for number in os.listdir('/proc/self/fd'):
            fd=int(number)
            if fd>2:
                try:os.close(fd)
                except OSError:pass
        os.chdir(root)
        print(f'wiki session: read-only; Landlock ABI {abi}; output={allowed[0]}',file=sys.stderr,flush=True)
        os.execvpe(command[0],command,env)
    except (ValueError,OSError) as exc:
        print(f'wiki session refused: {exc}',file=sys.stderr);return 2


if __name__=='__main__':sys.exit(main())
