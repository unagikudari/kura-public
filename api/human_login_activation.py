"""Concrete bounded operator activation while source and target transactions live.

No generic authorization callback: actual declared unit/config/build and login
responses are checked. The target SQL commit is the credential authority cut.
"""
from __future__ import annotations
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import select
import subprocess
import tempfile
import sys
import time
import urllib.parse

from api.human_login import KeyRefused, key_digest
from scripts.human_login_source import environment


_HTTP_PROBE = r"""
import base64,json,sys,urllib.request,urllib.error
value=json.load(sys.stdin)
headers={'Content-Type':'application/json'}
if value['token'] is not None:
    headers['Authorization']='Token '+base64.b64encode(value['token'].encode()).decode()
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
request=urllib.request.Request(value['url'],
    data=json.dumps(value['body']).encode() if value['body'] is not None else None,headers=headers)
try:
    try:response=opener.open(request,timeout=5)
    except urllib.error.HTTPError as error:response=error
    with response:
        raw=response.read(65537)
        if len(raw)>65536:raise ValueError('response_size')
        print(json.dumps({'status':response.code,'body':json.loads(raw)}))
except Exception:
    raise SystemExit(2)
"""


def private_bytes(path):
    import stat
    st=path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode & 0o077:
        raise KeyRefused('private_activation_input_required')
    return path.read_bytes()


def write_private(path, data, *, exclusive):
    if exclusive:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        temporary=None
    else:
        private_bytes(path)
        fd,name=tempfile.mkstemp(prefix='.kura-auth-',dir=path.parent)
        temporary=Path(name)
    try:
        with os.fdopen(fd,'wb') as file:
            file.write(data);file.flush();os.fsync(file.fileno())
        if temporary is not None:os.replace(temporary,path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


class Activation:
    def __init__(self, *, environment_file, staged_environment, unit, api_url,
                 key_file, revision, timeout, input_fd, record):
        if not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service',unit):
            raise KeyRefused('invalid_api_unit')
        parsed=urllib.parse.urlsplit(api_url)
        try:loopback=ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:loopback=False
        if (parsed.scheme!='http' or not loopback or parsed.username or parsed.password
                or parsed.path not in ('','/') or parsed.query or parsed.fragment):
            raise KeyRefused('loopback_api_required')
        if not re.fullmatch(r'[0-9a-f]{40}',revision) or not math.isfinite(timeout) or timeout<=0:
            raise KeyRefused('activation_bounds_required')
        self.environment_file=Path(environment_file);self.staged_environment=Path(staged_environment)
        self.original=private_bytes(self.environment_file)
        if environment(self.environment_file).get('KURA_HUMAN_LOGIN_MODE','active')!='bootstrap':
            raise KeyRefused('bootstrap_required')
        lines=self.original.decode().splitlines()
        lines=[line for line in lines if not re.match(r'\s*(?:export\s+)?KURA_HUMAN_LOGIN_MODE\s*=',line)]
        self.staged=('\n'.join(lines)+'\nKURA_HUMAN_LOGIN_MODE=active\n').encode()
        self.raw=private_bytes(Path(key_file)).decode().strip();self.digest=key_digest(self.raw)
        self.unit,self.api_url,self.revision=unit,api_url.rstrip('/'),revision
        self.deadline=time.monotonic()+timeout;self.input_fd=input_fd;self.record=record
        self.commit_started=False;self.committed=False;self.prepared=False
        self.email=None;self.cut=None;self.target=None

    def remaining(self):
        left=self.deadline-time.monotonic()
        if left<=0:raise KeyRefused('activation_deadline')
        return left

    def unit_state(self):
        result=subprocess.run(['systemctl','--user','show',self.unit,
            '--property=EnvironmentFiles,WorkingDirectory,ActiveState,MainPID'],
            check=True,capture_output=True,text=True,timeout=min(5,self.remaining()))
        props=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
        if (props.get('EnvironmentFiles')!=str(self.environment_file)+' (ignore_errors=no)'
                or props.get('ActiveState')!='active' or int(props.get('MainPID','0'))<=0):
            raise KeyRefused('api_unit_binding_mismatch')
        head=subprocess.run(['git','-C',props['WorkingDirectory'],'rev-parse','HEAD'],
            check=True,capture_output=True,text=True,timeout=min(5,self.remaining())).stdout.strip()
        if head!=self.revision:raise KeyRefused('api_checkout_mismatch')
        return {'unit':self.unit,'pid':int(props['MainPID']),'revision':head}

    def request(self,path,body=None,token=None):
        # A private child gives the *whole* HTTP exchange a hard remaining
        # deadline, including slow-drip headers/body. Secrets travel only by pipe.
        payload={"url":self.api_url+path,"body":body,"token":token}
        try:
            result=subprocess.run([sys.executable,'-I','-c',_HTTP_PROBE],
                input=json.dumps(payload),capture_output=True,text=True,check=True,
                timeout=min(5,self.remaining()),env={"LANG":"C.UTF-8"})
            value=json.loads(result.stdout)
            return value['status'],value['body']
        except (subprocess.SubprocessError,OSError,ValueError,KeyError):
            raise KeyRefused('activation_http_unavailable') from None

    def build(self):
        unit=self.unit_state()
        status,value=self.request('/api/build')
        short=value.get('commit','') if isinstance(value,dict) else ''
        if (status!=200 or not re.fullmatch(r'[0-9a-f]{7,40}',short)
                or not self.revision.startswith(short) or value.get('dirty') is not False):
            raise KeyRefused('api_build_mismatch')
        return unit

    def login_status(self):
        return self.request('/api/auth/login',{'email':self.email,'key':self.raw})

    def prepare(self,cut,target):
        matches=[key for key in cut.keys if key.key_digest==self.digest]
        if len(matches)!=1:raise KeyRefused('activation_key_not_in_cut')
        self.email=matches[0].email;self.cut=cut;self.target=target
        cut.assert_held(deadline=self.deadline);self.build()
        status,body=self.login_status()
        if status!=503 or body.get('detail')!='credential_handoff_in_progress':
            raise KeyRefused('bootstrap_not_proven')
        write_private(self.staged_environment,self.staged,exclusive=True)
        self.prepared=True
        self.record({'operation':'import','state':'awaiting_activation','target':target,
                     'source_cut':cut.receipt(deadline=self.deadline),'staged_environment_digest':'sha256:'+hashlib.sha256(self.staged).hexdigest()})
        print(json.dumps({'operation':'import','state':'awaiting_activation',
                          'staged_environment':str(self.staged_environment)}),flush=True)
        # Root/operator installs exactly the staged bytes and restarts the named unit.
        # Keys remain invisible until the enclosing import transaction commits.
        command=bytearray()
        while b'\n' not in command:
            cut.assert_held(deadline=self.deadline)
            if not select.select([self.input_fd],[],[],min(1,self.remaining()))[0]:continue
            part=os.read(self.input_fd,64)
            if not part:raise KeyRefused('activation_aborted')
            command.extend(part)
            if len(command)>64:raise KeyRefused('activation_command_invalid')
        if bytes(command)!=b'activate\n':raise KeyRefused('activation_aborted')
        cut.assert_held(deadline=self.deadline)
        if private_bytes(self.environment_file)!=self.staged:raise KeyRefused('staged_config_mismatch')
        self.build()
        status,body=self.login_status()
        if status!=401 or body.get('detail')!='invalid_key':
            raise KeyRefused('uncommitted_key_not_isolated')
        cut.assert_held(deadline=self.deadline);self.remaining()

    def verify_committed(self):
        self.cut.assert_held(deadline=self.deadline);unit=self.build()
        status,body=self.login_status()
        if status!=200 or not isinstance(body,dict) or not body.get('username') or not body.get('token'):
            raise KeyRefused('committed_login_not_proven')
        token=body['username']+':'+body['token']
        try:
            if body.get('email','').lower()!=self.email:
                raise KeyRefused('committed_login_not_proven')
            status,identity=self.request('/api/auth/whoami',token=token)
            if status!=200 or identity.get('email','').lower()!=self.email:
                raise KeyRefused('committed_identity_not_proven')
        finally:
            status,_=self.request('/api/auth/logout',{},token=token)
            if status!=200:raise KeyRefused('probe_session_revoke_unconfirmed')
        self.cut.assert_held(deadline=self.deadline);return unit

    def abort_before_commit(self):
        if self.commit_started:raise KeyRefused('committed_or_uncertain_cut_requires_reconciliation')
        if not self.prepared:return
        write_private(self.environment_file,self.original,exclusive=False)
        subprocess.run(['systemctl','--user','restart',self.unit],check=True,
                       capture_output=True,timeout=min(10,self.remaining()))
        self.build()
        status,body=self.login_status()
        if status!=503 or body.get('detail')!='credential_handoff_in_progress':
            raise KeyRefused('abort_blocked_state_unproven')
