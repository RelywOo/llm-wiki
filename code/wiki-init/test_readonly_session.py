"""Kernel-enforced inspection through the installed public process launcher."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE=Path(__file__).resolve().parent


@unittest.skipUnless(sys.platform=='linux','Linux session enforcement')
class ReadOnlySessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='wiki-inspection-');self.addCleanup(self.tmp.cleanup)
        base=Path(self.tmp.name);self.root=base/'project';self.root.mkdir();self.output=base/'output';self.output.mkdir()
        shutil.copytree(HERE/'templates/tree/.memory/tools',self.root/'.memory/tools',ignore=shutil.ignore_patterns('__pycache__'))
        (self.root/'.memory/STATE.md').write_text('Original state.\n')
        subprocess.run(['git','init','-q',str(self.root)],check=True)
        subprocess.run(['git','-C',str(self.root),'config','user.name','Test'],check=True)
        subprocess.run(['git','-C',str(self.root),'config','user.email','test@example.invalid'],check=True)
        subprocess.run(['git','-C',str(self.root),'add','.'],check=True)
        subprocess.run(['git','-C',str(self.root),'commit','-qm','Initial state'],check=True)

    def run_session(self,command,extra=(),**kwargs):
        return subprocess.run([sys.executable,'.memory/tools/readonly_session.py','--root',str(self.root),
                               '--output',str(self.output),*extra,'--',*command],cwd=self.root,
                              text=True,capture_output=True,**kwargs)

    def test_reading_works_direct_memory_write_is_blocked_and_write_mode_recovers(self):
        command=[sys.executable,'-c',"from pathlib import Path; p=Path('.memory/STATE.md'); print(p.read_text()); p.write_text('Wrong')"]
        result=self.run_session(command)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Original state.',result.stdout)
        self.assertIn('read-only',result.stderr)
        self.assertEqual((self.root/'.memory/STATE.md').read_text(),'Original state.\n')
        ordinary=subprocess.run(command,cwd=self.root,text=True,capture_output=True)
        self.assertEqual(ordinary.returncode,0,ordinary.stderr)
        self.assertEqual((self.root/'.memory/STATE.md').read_text(),'Wrong')

    def test_read_session_cannot_delegate_a_write_to_an_existing_unix_service(self):
        import socket
        import threading
        endpoint=self.root.parent/'writer.sock'
        server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);server.bind(str(endpoint));server.listen();server.settimeout(0.6)
        delegated=[]
        def receive():
            try:
                client,_=server.accept()
                with client:
                    if client.recv(32)==b'write':
                        (self.root/'.memory/STATE.md').write_text('Delegated write')
                        delegated.append(True)
            except TimeoutError:pass
        thread=threading.Thread(target=receive);thread.start()
        try:
            result=self.run_session([sys.executable,'-c',
                "import socket; s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.connect("+repr(str(endpoint))+"); s.sendall(b'write')"])
            thread.join()
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(delegated,[])
            self.assertEqual((self.root/'.memory/STATE.md').read_text(),'Original state.\n')
        finally:server.close();thread.join()


    def test_inherited_writable_descriptor_cannot_bypass_the_session(self):
        fd=os.open(self.root/'.memory/STATE.md',os.O_WRONLY)
        try:
            result=self.run_session([sys.executable,'-c',f"import os; os.write({fd},b'Bypass')"],pass_fds=(fd,))
            self.assertNotEqual(result.returncode,0)
            self.assertEqual((self.root/'.memory/STATE.md').read_text(),'Original state.\n')
        finally:os.close(fd)

    def test_external_shared_memory_cannot_be_modified(self):
        import ctypes as C
        libc=C.CDLL(None,use_errno=True)
        identifier=libc.shmget(0,4096,0o600|0o1000)
        if identifier<0:self.skipTest('SysV shared memory unavailable')
        libc.shmat.restype=C.c_void_p
        pointer=libc.shmat(identifier,None,0)
        self.assertNotEqual(pointer,C.c_void_p(-1).value)
        try:
            C.memmove(pointer,b'Original',8)
            script=f"import ctypes as C; lib=C.CDLL(None); lib.shmat.restype=C.c_void_p; p=lib.shmat({identifier},None,0); assert p!=C.c_void_p(-1).value; C.memmove(p,b'Changed!',8)"
            result=self.run_session([sys.executable,'-c',script])
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(C.string_at(pointer,8),b'Original')
        finally:
            libc.shmdt(C.c_void_p(pointer));libc.shmctl(identifier,0,None)


    def test_writable_directory_cannot_hide_a_hard_link_to_memory(self):
        os.link(self.root/'.memory/STATE.md',self.output/'alias.txt')
        result=self.run_session([sys.executable,'-c',"from pathlib import Path; Path("+repr(str(self.output/'alias.txt'))+").write_text('Alias write')"])
        self.assertNotEqual(result.returncode,0)
        self.assertEqual((self.root/'.memory/STATE.md').read_text(),'Original state.\n')


    def test_git_index_refs_and_commit_mutations_are_blocked(self):
        head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=self.root,text=True).strip()
        before=(self.root/'.git/index').read_bytes()
        for command in (['git','add','.'],['git','update-ref','refs/heads/blocked',head],
                        ['git','-c','core.hooksPath=/dev/null','commit','--allow-empty','-m','Forbidden']):
            with self.subTest(command=command):
                result=self.run_session(command);self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertEqual(subprocess.check_output(['git','rev-parse','HEAD'],cwd=self.root,text=True).strip(),head)
                self.assertEqual((self.root/'.git/index').read_bytes(),before)
        self.assertEqual(self.run_session(['git','status','--porcelain']).returncode,0)

    def test_file_metadata_cannot_change_through_paths_or_read_descriptors(self):
        target=self.root/'.memory/STATE.md'
        before=target.stat()
        scripts=["os.chmod(p,0o777)", "os.utime(p,ns=(1,1))",
                 "os.setxattr(p,'user.inspection',b'wrong')",
                 "fd=os.open(p,os.O_RDONLY); os.fchmod(fd,0o777)"]
        for operation in scripts:
            with self.subTest(operation=operation):
                result=self.run_session([sys.executable,'-c',"import os; p='.memory/STATE.md'; "+operation])
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                after=target.stat()
                self.assertEqual(after.st_mode,before.st_mode)
                self.assertEqual(after.st_mtime_ns,before.st_mtime_ns)
                self.assertNotIn('user.inspection',os.listxattr(target))

    def test_descendants_background_workers_environment_and_symlinks_do_not_escape(self):
        (self.output/'memory-link').symlink_to(self.root/'.memory')
        scripts=["import os; os.environ.pop('LLM_WIKI_SESSION',None); open('.memory/STATE.md','w').write('Wrong')",
                 "from pathlib import Path; Path("+repr(str(self.output/'memory-link/STATE.md'))+").write_text('Wrong')",
                 "from pathlib import Path; Path('.memory/STATE.md').unlink()",
                 "import os; os.rename('.memory/STATE.md','.memory/moved.md')"]
        for script in scripts:
            result=self.run_session([sys.executable,'-c',
                "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c',"+repr(script)+"],start_new_session=True); sys.exit(p.wait())"])
            self.assertNotEqual(result.returncode,0)
            self.assertEqual((self.root/'.memory/STATE.md').read_text(),'Original state.\n')
        self.assertFalse((self.root/'.memory/moved.md').exists())

    def test_output_and_temporary_work_are_writable_and_search_is_read_only(self):
        wiki=self.root/'.memory/wiki';wiki.mkdir()
        (wiki/'concepts').mkdir();(wiki/'concepts/example.md').write_text('# Quasar\nA quasar migration rule.\n')
        result=self.run_session([sys.executable,'.memory/tools/structural.py','search','quasar'])
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('quasar',result.stdout.lower())
        command=[sys.executable,'-c',"import os,tempfile; from pathlib import Path; Path(os.environ['LLM_WIKI_OUTPUT'],'answer.txt').write_text('Read result'); f=tempfile.TemporaryFile(); f.write(b'scratch')"]
        self.assertEqual(self.run_session(command).returncode,0)
        self.assertEqual((self.output/'answer.txt').read_text(),'Read result')
        self.assertFalse(any(p.suffix in {'.sqlite','.db','.pyc'} for p in self.root.rglob('*')))

    def test_writable_overlap_or_symlink_is_refused_before_child_execution(self):
        alias=self.root.parent/'alias';alias.symlink_to(self.output,target_is_directory=True)
        for writable in [self.root,self.root/'.memory',self.root.parent,self.root/'.git',alias]:
            result=self.run_session([sys.executable,'-c',"print('CHILD_EXECUTED')"],extra=('--writable',str(writable)))
            self.assertNotEqual(result.returncode,0)
            self.assertNotIn('CHILD_EXECUTED',result.stdout)

    def test_linked_worktree_git_storage_is_protected(self):
        linked=self.root.parent/'linked'
        subprocess.run(['git','-C',str(self.root),'worktree','add','-qb','linked',str(linked)],check=True)
        result=subprocess.run([sys.executable,str(linked/'.memory/tools/readonly_session.py'),'--root',str(linked),
            '--output',str(self.output),'--', 'git','update-ref','refs/heads/escaped','HEAD'],cwd=linked,text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertNotEqual(subprocess.run(['git','-C',str(self.root),'rev-parse','--verify','refs/heads/escaped'],capture_output=True).returncode,0)
        bad=subprocess.run([sys.executable,str(linked/'.memory/tools/readonly_session.py'),'--root',str(linked),
            '--output',str(self.root/'.git'),'--',sys.executable,'-c',"print('CHILD_EXECUTED')"],text=True,capture_output=True)
        self.assertNotEqual(bad.returncode,0);self.assertNotIn('CHILD_EXECUTED',bad.stdout)

    def test_network_is_opt_in_and_unix_delegation_stays_blocked(self):
        socket_probe=[sys.executable,'-c',"import socket; s=socket.socket(socket.AF_INET,socket.SOCK_STREAM); s.close()"]
        self.assertNotEqual(self.run_session(socket_probe).returncode,0)
        self.assertEqual(self.run_session(socket_probe,extra=('--network',)).returncode,0)
        self.assertNotEqual(self.run_session([sys.executable,'-c',"import socket; socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)"],extra=('--network',)).returncode,0)


    def test_symlink_backed_memory_cannot_be_declared_writable(self):
        shared=self.root.parent/'shared';shared.mkdir()
        (shared/'notes.md').write_text('Shared knowledge')
        (self.root/'.memory/shared').symlink_to(shared,target_is_directory=True)
        result=self.run_session([sys.executable,'-c',"print('CHILD_EXECUTED')"],extra=('--writable',str(shared)))
        self.assertNotEqual(result.returncode,0)
        self.assertNotIn('CHILD_EXECUTED',result.stdout)

    def test_unsupported_platform_refuses_before_running_the_child(self):
        script="import runpy,sys; sys.platform='unsupported'; sys.argv=['readonly_session.py','--root',"+repr(str(self.root))+",'--output',"+repr(str(self.output))+",'--',sys.executable,'-c',\"print('CHILD_EXECUTED')\"]; runpy.run_path('.memory/tools/readonly_session.py',run_name='__main__')"
        result=subprocess.run([sys.executable,'-c',script],cwd=self.root,text=True,capture_output=True)
        self.assertNotEqual(result.returncode,0)
        self.assertNotIn('CHILD_EXECUTED',result.stdout)
        self.assertIn('refusing unprotected execution',result.stderr)



if __name__=='__main__':unittest.main()
