#! /usr/bin/env python

"""
ArchiveFS, a FUSE filesystem for archival and backup storage.
Copyright (C) 2009 Thomas Breuel <www.9x9.com>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

# MISC NOTES
# - when something isn't working, check whether a function is returning
#   a unicode string; the Fuse API just fails with no error message

__author__ = "Thomas Breuel <www.9x9.com>"
__version__ = "0.0"
__license__ = "GNU General Public License (version 3)"

import os,sys,re,math,stat,errno,fuse,sqlite3,random,shutil,hashlib,time
from os.path import join,basename,relpath
from os.path import split as splitpath
from subprocess import *
import sqlite3
import logging

def ndirname(s):
    """Normalized dirname is like regular dirname, but
    it returns "" for the root directory (rather than "/",
    which is inconsistent)."""
    dir = os.path.dirname(s)
    if dir=="/": dir = ""
    return dir
def nnormpath(s):
    """Normalized dirname is like regular dirname, but
    it returns "" for the root directory (rather than "/",
    which is inconsistent)."""
    path = os.path.normpath(s)
    if path=="/": path = ""
    return path

fuse.fuse_python_api = (0, 2)

debug_flag = (os.getenv("debug") is not None)
verbose_flag = (os.getenv("verbose") is not None)

log = logging.getLogger()
ch = logging.StreamHandler()
log.addHandler(ch)

if os.getenv("log")=="debug": log.setLevel(logging.DEBUG)
elif os.getenv("log")=="info": log.setLevel(logging.INFO)

if os.getenv("logfile") is not None:
    fh = logging.FileHandler(os.getenv("logfile"))
    log.addHandler(fh)

def md5sum_old(rpath):
    id = os.popen("md5sum %s | cut -f 1 -d ' '" % rpath).read()
    id = tag[:-1]
    return id

def md5sum(rpath):
    h = hashlib.md5()
    with open(rpath) as stream:
        while 1:
            data = stream.read(100000)
            if len(data)<1: break
            h.update(data)
    return h.hexdigest()
                
def md5hex(data):
    h = hashlib.md5()
    h.update(data)
    return h.hexdigest()

def flags2mode(flags):
    md = {os.O_RDONLY: 'r', os.O_WRONLY: 'w', os.O_RDWR: 'w+'}
    m = md[flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR)]
    if flags & os.O_APPEND:
        m = m.replace('w', 'a', 1)
    return m+"b"

class MyStat(fuse.Stat):
    def __init__(self,mode=0):
        now = time.time()
        self.st_mode = mode
        self.st_ino = 0
        self.st_atime = now
        self.st_mtime = now
        self.st_ctime = now
        self.st_size = 0
        self.st_uid = 0
        self.st_gid = 0
        self.st_blocks = 0
        self.st_rdev = 0
        self.st_dev = 0
        self.st_nlink = 2
        self.st_blksize = 4096
        self.id = None

open_files = {}

class SqlFileStore:
    """An abstraction that encapsulates the 'file system' semantics;
    methods are somewhat different from POSIX, since this actually
    implements the archive file system (combination of database and
    file system storage)."""
    def __init__(self,base):
        self.DBFILE = join(base+"/DB")
        self.ARCHIVE = join(base+"/ARCHIVE")
        self.WORKING = join(base+"/WORKING")
        self.conn = sqlite3.connect(self.DBFILE,timeout=600.0)
        self.conn.row_factory = sqlite3.Row
        try: self.make_tables()
        except: pass
        self.mkentry("/",mode=0777|stat.S_IFDIR)
        if not os.path.exists(self.ARCHIVE):
            os.mkdir(self.ARCHIVE)
        if not os.path.exists(self.WORKING):
            os.mkdir(self.WORKING)
    def working_path(self,path):
        return join(self.WORKING,md5hex(path))
    def is_working(self,path):
        return path[:len(self.WORKING)]==self.WORKING
    def rename(self,path,newpath):
        self.set(path,"path",newpath)
    def archive_path(self,id):
        assert "/" not in id
        dir = re.sub(r'(..)(..).*','\\1/\\2',id)
        destdir = join(self.ARCHIVE,dir)
        if not os.path.exists(destdir):
            os.makedirs(destdir)
        return join(destdir,id)
    def make_tables(self):
        """Create the initial database tables."""
        c = self.conn.cursor()
        c.execute("""
        create table files (
        path text primary key,
        dir text,
        id text,
        mode integer,
        size integer,
        atime real,
        mtime real,
        ctime real,
        symlink text
        )
        """)
        c.execute("create index id on files (id)")
        c.execute("create index dir on files (dir)")
        self.conn.commit()
        c.close()
    def entry(self,path,check=1):
        path = nnormpath(path)
        c = self.conn.cursor()
        c.execute("select * from files where path=?",(path,))
        entry = c.fetchone()
        if check and entry is None:
            raise IOError(errno.ENOENT,path)
        c.close()
        return entry
    def set(self,path,key,value,check=1):
        path = nnormpath(path)
        c = self.conn.cursor()
        c.execute("update files set %s=? where path=?"%key,(value,path))
        self.conn.commit()
        assert c.rowcount<2,"set %s %s %s"%(path,key,value)
        if check and c.rowcount<1:
            raise IOError(errno.ENOENT,path)
        c.close()
    def get(self,path,key,check=1):
        path = nnormpath(path)
        c = self.conn.cursor()
        c.execute("select %s from files where path=?"%key,(path,))
        row = c.fetchone()
        if check and row is None:
            raise IOError(errno.ENOENT,path)
        c.close()
        if row is None: return None
        return row[0]
    def instances(self,id):
        c = self.conn.cursor()
        c.execute("select path from files where id=?",(id,))
        for row in c:
            yield row[0]
        c.close()
    def mode(self,path):
        return self.get(path,"mode")
    def exists(self,path):
        return (self.get(path,"path",check=0) is not None)
    def isdir(self,path):
        """Check whether the given path is a directory."""
        mode = self.mode(path,check=0)
        if mode is None: return 0
        return mode&stat.S_IFDIR
    def checkdir(self,path):
        dir = ndirname(path)
        if not self.isdir(path):
            raise IOError(errno.EINVAL,path)
    def delete(self,path):
        """Delete the given path unconditionally (no checks)."""
        log.debug("delete %s",path)
        path = nnormpath(path)
        c = self.conn.cursor()
        c.execute("delete from files where path=?",(path,))
        self.conn.commit()
        c.close()
    def rmdir(self,path):
        """Delete a directory, with the usual UNIX checks."""
        log.debug("rmdir %s",path)
        path = nnormpath(path)
        c = self.conn.cursor()
        c.execute("select * from files where path=?",(path,))
        if c.fetchone() is None: raise IOError(errno.ENOENT,path)
        c.execute("select * from files where path>=? and path<?",(path+"/",path+chr(ord("/")+1)))
        if c.fetchone() is not None: raise IOError(errno.ENOTEMPTY,path)
        c.execute("delete from files where path=?",(path,))
        self.conn.commit()
        c.close()
    def listdir(self,path):
        """Return a list of the entries in the given directory."""
        dir = nnormpath(path)
        entries = [ '.', '..' ]
        c = self.conn.cursor()
        c.execute("select * from files where dir=?",(dir,))
        dir += "/"
        prefix = len(dir)
        for file in c:
            name = file["path"][prefix:]
            if name=="": continue
            entries += [name]
        for entry in entries:
            yield entry.encode("utf8")
        c.close()
    def chmod(self,path,mode):
        old = self.get(path,"mode")
        mode = (old&~0777)|(mode&0777)
        self.set(path,"mode",mode)
    def utime(self,path,atime,mtime):
        self.set(path,"atime",atime)
        self.set(path,"mtime",mtime)
    def chown(self,path,user):
        return
    def mkentry(self,path,mode=0666|stat.S_IFREG,when=time.time(),id=None,symlink=None):
        """Make a new path entry for the given path and with the given mode.
        Uses the current time for all the file times."""
        log.debug("mkentry %s %s %s %s",path,mode,id,symlink)
        assert id is None or re.match(r'^[0-9a-z]+$',id)
        path = nnormpath(path)
        dir = ndirname(path)
        c = self.conn.cursor()
        c.execute("""
            insert or replace into files
            (path,mode,atime,mtime,ctime,id,symlink,dir)
            values (?,?,?,?,?,?,?,?)
        """,(path,mode,when,when,when,id,symlink,dir))
        self.conn.commit()
        c.close()
    def symlink(self,content,path):
        self.mkentry(path,mode=stat.S_IFLNK|0777,symlink=content)
    def readlink(self,path):
        content = self.get(path,"symlink")
        if content is None: raise IOError(errno.EINVAL,path)
        content = content.encode("utf8")
        return content
    def getattr(self,path):
        entry = self.entry(path)
        st = MyStat()
        st.st_mode = entry["mode"]
        st.st_atime = int(entry["atime"])
        st.st_mtime = int(entry["mtime"])
        st.st_ctime = int(entry["ctime"])
        st.id = entry["id"]
        return st

class ArchiveFS(fuse.Fuse):
    def __init__(self, *args, **kw):
        fuse.Fuse.__init__(self, *args, **kw)
        try:
            make_tables()
            self.mkdir("/",0777)
        except:
            pass
        self.files = {}
    def main(self,*a,**kw):
        self.fs = SqlFileStore(self.root)
        return fuse.Fuse.main(self,*a,**kw)
    def getattr(self, path):
        st = self.fs.getattr(path)
        active = self.files.get(path)
        if active is not None:
            stream,rpath = active
            base = os.fstat(stream.fileno())
            st.st_size = base.st_size
            st.st_blocks = base.st_blocks
            st.st_blksize = base.st_blksize
            st.st_atime = base.st_atime
            st.st_mtime = base.st_mtime
            log.debug("getattr %s active %s %s %s",path,rpath,st.st_atime,st.st_mtime)
            return st
        if st.id is not None:
            base = os.lstat(self.fs.archive_path(st.id))
            st.st_size = base.st_size
            st.st_blocks = base.st_blocks
            st.st_blksize = base.st_blksize
            log.debug("getattr %s id %s %s %s",path,st.id,st.st_atime,st.st_mtime)
            return st
        log.debug("getattr %s default",path)
        return st
    def readdir(self, path, offset):
        for entry in self.fs.listdir(path):
            yield fuse.Direntry(entry)
    def mkdir(self, path, mode):
        if self.fs.exists(path): return -errno.EEXISTS
        self.fs.mkentry(path,mode=mode|stat.S_IFDIR)
        return 0
    def access(self,path,which):
        mode = self.fs.mode(path)
        return 0
    def rmdir(self, path):
        self.fs.rmdir(path)
        return 0
    def mknod(self, path, mode, dev):
        return 0
    def unlink(self, path):
        self.fs.delete(path)
        return 0
    def rename(self, pathfrom, pathto):
        self.fs.rename(pathfrom,pathto)
        return 0
    def truncate(self,path,len):
        if len>0: 
            log.warning("truncate not implemented %s %s",path,len)
            raise IOError(errno.ENOSYS,path)
        self.fs.delete(path)
        self.fs.mkentry(path)
        return 0
    def chmod(self,path,mode):
        self.fs.chmod(path,mode)
        return 0
    def chown(self,path,*args):
        return 0
    def utime(self,path,times=None):
        if times == None:
            now = time.time()
            times = (now,now)
        self.fs.utime(path,times[0],times[1])
    def readlink(self,path):
        content = self.fs.readlink(path)
        log.debug("readlink %s -> %s",path,content)
        return content
    def symlink(self,content,path):
        log.debug("symlink %s %s",content,path)
        # fs.checkdir(path)
        self.fs.symlink(content,path)
        return 0

    # here come the actual file operations
    
    def osopen(self,path,flags,mode=0666):
        return os.fdopen(os.open(path,flags,mode),flags2mode(flags))
    def open(self,path,flags):
        log.debug("open %s %s",path,flags)
        if flags&2:
            id = self.fs.get(path,"id")
            rpath = self.fs.working_path(path)
            if id is not None:
                old = self.fs.archive_path(id)
                log.debug("copying %s %s",old,rpath)
                shutil.copyfile(old,rpath)
            self.files[path] = (self.osopen(rpath,flags),rpath)
        else:
            id = self.fs.get(path,"id")
            if id is not None:
                rpath = self.fs.archive_path(id)
            else:
                rpath = "/dev/null"
            self.files[path] = (self.osopen(rpath,flags),rpath)
        return 0
    def create(self,path,flags,mode):
        log.debug("create %s %s %s",path,flags,mode)
        self.fs.delete(path)
        self.fs.mkentry(path,mode=(mode&07777)|stat.S_IFREG)
        rpath = self.fs.working_path(path)
        self.files[path] = (self.osopen(rpath,flags),rpath)
        return 0
    def release(self,path,flags):
        log.debug("release %s %s",path,flags)
        stream,rpath = self.files.get(path)
        del self.files[path]
        stream.close()
        if self.fs.is_working(rpath):
            id = md5sum(rpath)
            dest = self.fs.archive_path(id)
            if os.path.exists(dest):
                log.debug("EXISTS %s for %s",dest,path)
                os.unlink(rpath)
            else:
                os.chmod(rpath,0400)
                shutil.move(rpath,dest)
                log.debug("moved %s %s for %s",rpath,dest,path)
            self.fs.set(path,"id",id)
            self.utime(path)
        log.debug("release %s done")
        return 0
    def fgetattr(self,path,fh=None):
        # log.debug("fgetattr",path)
        return self.getattr(path)
    def read(self,path,length,offset,fh=None):
        # log.debug("read",path,length,offset)
        stream,rpath = self.files.get(path)
        stream.seek(offset)
        return stream.read(length)
    def write(self,path,buf,offset,fh=None):
        # log.debug("write",path,len(buf),offset)
        stream,rpath = self.files.get(path)
        stream.seek(offset)
        stream.write(buf)
        return len(buf)
    def ftruncate(self,path,len,fh=None):
        # log.debug("ftruncate",path,len)
        stream,rpath = self.files.get(path)
        stream.truncate(len)
        return 0
    def fsync(self,path,fdatasync,fh=None):
        # log.debug("fsync",path)
        return 0
    def flush(self,path):
        # log.debug("flush",path)
        stream,rpath = self.files.get(path)
        stream.flush()
        return 0
    def statfs(self):
        return os.statvfs(self.fs.DBFILE)
    def getxattr_(self,path,key):
        log.debug("getxattr_ %s %s",path,key)
        if key=="user._id":
            return self.fs.get(path,"id")
        elif key=="user._storage":
            return self.fs.archive_path(self.fs.get(path,"id"))
        elif key=="user._instances":
            result = ""
            for instance in self.fs.instances(self.fs.get(path,"id")):
                if result!="": result += "\n"
                result += instance
            return result
        else:
            raise IOError(errno.EINVAL,path)
    def listxattr_(self,path):
        return ["user._id","user._storage","user._instances"]
    def setxattr_(self,path,key,value):
        raise IOError(errno.EINVAL,path)
    def getxattr(self,path,key,size):
        s = self.getxattr_(path,key).encode("utf8")
        log.debug("getxattr %s %s %d %s",path,key,size,re.sub(r'\n','|',s)[:40])
        if size==0: 
            return len(s)
        else:
            return s
    def listxattr(self,path,size):
        l = self.listxattr_(path)
        log.debug("listxattr %s %d %s",path,size,l)
        if size==0: 
            return len(l)+len("".join(l))
        else:
            return l

#         sfs = fuse.StatVfs()
#         sfs.f_bsize = 1024
#         sfs.f_frsize = 1024
#         sfs.f_block = 0
#         sfs.f_free = 0
#         sfs.f_avail = 0
#         return sfs

def main():
    usage="""ArchiveFS: an archival file system that stores only
    single copies of the same file.""" + fuse.Fuse.fusage

    server = ArchiveFS(version="%prog "+fuse.__version__,
                       usage=usage, dash_s_do='setsingle')
    server.parser.add_option(mountopt="root",metavar="PATH",default="/tmp/TEST",help="storage directory")
    server.parse(values=server,errex=1)
    server.flags = 0
    server.multithreaded = 0
    server.main()

if __name__ == '__main__':
    main()
