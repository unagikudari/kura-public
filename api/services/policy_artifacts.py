"""Bounded, stat-aware availability observations for policy metadata only.

ArtifactStore.have/get/verify_all retain their full byte verification. A policy
poll reuses a recent observation for an unchanged file; writes/replacement or
expiry require a fresh hash. No filesystem/hash work runs on the event loop.
"""
import asyncio
import stat
import threading
import time
from collections import OrderedDict

class PolicyArtifacts:
    def __init__(self,ttl=600,capacity=256,clock=time.monotonic):
        self.ttl=ttl;self.capacity=capacity;self.clock=clock
        self.entries=OrderedDict();self.lock=threading.Lock()
    @staticmethod
    def identity(path):
        s=path.stat()
        if not stat.S_ISREG(s.st_mode):raise ValueError('not an artifact file')
        return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
    def check(self,store,hashes):
        with self.lock: # coalesce concurrent cold scans without blocking asyncio
            out={}
            for sha in dict.fromkeys(hashes):
                path=store.path_for(sha);key=str(path.absolute())
                try:identity=self.identity(path)
                except (OSError,ValueError):self.entries.pop(key,None);out[sha]=False;continue
                old=self.entries.get(key)
                if old and old[0]==identity and old[1]>self.clock():
                    self.entries.move_to_end(key);out[sha]=old[2];continue
                try:valid=store.have(sha) # full verification on first use/change/expiry
                except OSError:self.entries.pop(key,None);out[sha]=False;continue
                try:stable=self.identity(path)==identity
                except (OSError,ValueError):stable=False
                if not stable:self.entries.pop(key,None);out[sha]=False;continue
                self.entries[key]=(identity,self.clock()+self.ttl,valid)
                self.entries.move_to_end(key)
                while len(self.entries)>self.capacity:self.entries.popitem(last=False)
                out[sha]=valid
            return out

_observations=PolicyArtifacts()
async def policy_artifact_availability(store,hashes):
    return await asyncio.to_thread(_observations.check,store,tuple(hashes))
