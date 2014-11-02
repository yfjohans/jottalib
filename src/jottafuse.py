#!/usr/bin/env python
# -*- encoding: utf-8 -*-
#
# This file is part of jottafs.
# 
# jottafs is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# jottafs is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with jottafs.  If not, see <http://www.gnu.org/licenses/>.
# 
# Copyright 2011,2013,2014 Håvard Gulldahl <havard@gulldahl.no>

# metadata

__author__ = 'havard@gulldahl.no'

# importing stdlib
import sys, os, pwd, stat, errno
import urllib, logging, datetime
import time
import itertools
try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO


# import jotta
from jottalib import JFS

# import dependenceis (get them with pip!)
try:
    from fuse import FUSE, Operations, LoggingMixIn # this is 'pip install fusepy'
except ImportError:
    print "JottaFuse won't work without fusepy! Please run `pip install fusepy`."
    raise

class JottaFuseError(OSError):
    pass

BLACKLISTED_FILENAMES = ('.hidden', '._', '.DS_Store', '.Trash', '.Spotlight-', '.hotfiles-btree',
                         'lost+found', 'Backups.backupdb', 'mach_kernel')

def is_blacklisted(path):
    _basename = os.path.basename(path)
    for bf in BLACKLISTED_FILENAMES:
        if _basename.startswith(bf):
            return True
    return False

class JottaFuse(LoggingMixIn, Operations):
    '''
    A simple filesystem for JottaCloud.

    '''

    def __init__(self, username, password, path='.'):
        self.client = JFS.JFS(username, password)
        self.root = path
        self.dirty = False # True if some method has changed/added something and we need to get fresh data from JottaCloud
        # TODO: make self.dirty more smart, to know what path, to get from cache and not
        self.__newfiles = []
        self.__newfolders = []

    def _getpath(self, path):
        "A wrapper of JFS.getObject(), with some tweaks that make sense in a file system."
        if is_blacklisted(path):
            raise JottaFuseError('Blacklisted file, refusing to retrieve it')

        return self.client.getObject(path, usecache=self.dirty is not True)

    def create(self, path, mode):
        if is_blacklisted(path):
            raise JottaFuseError('Blacklisted file')
        if not path in self.__newfiles:
            self.__newfiles.append(path)
        return self.__newfiles.index(path)
        #return 0

    def destroy(self, path):
        #self.client.close()
        pass

    def getattr(self, path, fh=None):
        pw = pwd.getpwuid( os.getuid() )
        if path in self.__newfolders: # folder was just created, not synced yet
            return {
                'st_atime': time.time(),
                'st_gid': pw.pw_gid,
                'st_mode': stat.S_IFDIR | 0755, 
                'st_mtime': time.time(),
                'st_size': 0,
                'st_uid': pw.pw_uid,
                }
        elif path in self.__newfiles: # file was just created, not synced yet
            return {
                'st_atime': time.time(),
                'st_gid': pw.pw_gid,
                'st_mode': stat.S_IFREG | 0444,  
                'st_mtime': time.time(),
                'st_size': 0,
                'st_uid': pw.pw_uid,
                }
        try:
            f = self._getpath(path)
        except JFS.JFSError:
            raise OSError(errno.ENOENT, '') # can't help you

        if isinstance(f, JFS.JFSFile): 
            _mode = stat.S_IFREG | 0444
        elif isinstance(f, JFS.JFSFolder):
            _mode = stat.S_IFDIR | 0755
        elif isinstance(f, (JFS.JFSMountPoint, JFS.JFSDevice) ):
            _mode = stat.S_IFDIR | 0555 # read only dir
        else:
            logging.warning('Unknown jfs object: %s' % type(f) )
            _mode = stat.S_IFDIR | 0555
        return {
                'st_atime': isinstance(f, JFS.JFSFile) and time.mktime(f.updated.timetuple()) or time.time(),
                'st_gid': pw.pw_gid,
                'st_mode': _mode, 
                'st_mtime': isinstance(f, JFS.JFSFile) and time.mktime(f.modified.timetuple()) or time.time(),
                'st_size': isinstance(f, JFS.JFSFile) and f.size  or 0,
                'st_uid': pw.pw_uid,
                }

    def mkdir(self, path, mode):
        parentfolder = os.path.dirname(path)
        newfolder = os.path.basename(path)
        try:
            f = self._getpath(parentfolder)
        except JFS.JFSError:
            raise OSError(errno.ENOENT, '')
        if not isinstance(f, JFS.JFSFolder):
            raise OSError(errno.EACCES) # can only create stuff in folders
        r = f.mkdir(newfolder)
        self.dirty = True
        self.__newfolders.append(path)

    def read(self, path, size, offset, fh):
        if path in self.__newfiles: # file was just created, not synced yet
            return ''
        try:
            f = StringIO(self._getpath(path).read())
        except JFS.JFSError:
            raise OSError(errno.ENOENT, '')
        f.seek(offset, 0)
        buf = f.read(size)
        f.close()
        return buf

    def readdir(self, path, fh):
        yield '.'
        yield '..'
        if path == '/':
            for d in self.client.devices:
                yield d.name
        else:
            p = self._getpath(path)
            if isinstance(p, JFS.JFSDevice):
                for name in p.mountPoints.keys():
                    yield name
            else:    
                for el in itertools.chain(p.folders(), p.files()):
                    if not el.is_deleted():
                        yield el.name

    def statfs(self, path):
        "Return a statvfs(3) structure, for stat and df and friends"
        # from fuse.py source code:
        # 
        # class c_statvfs(Structure):
        # _fields_ = [
        # ('f_bsize', c_ulong), # preferred size of file blocks, in bytes
        # ('f_frsize', c_ulong), # fundamental size of file blcoks, in bytes
        # ('f_blocks', c_fsblkcnt_t), # total number of blocks in the filesystem
        # ('f_bfree', c_fsblkcnt_t), # number of free blocks
        # ('f_bavail', c_fsblkcnt_t), # free blocks avail to non-superuser
        # ('f_files', c_fsfilcnt_t), # total file nodes in file system
        # ('f_ffree', c_fsfilcnt_t), # free file nodes in fs
        # ('f_favail', c_fsfilcnt_t)] # 
        #
        # On Mac OS X f_bsize and f_frsize must be a power of 2
        # (minimum 512).

        _blocksize = 512
        _usage = self.client.usage
        _fs_size = self.client.capacity 
        if _fs_size == -1: # unlimited
            # Since backend is supposed to be unlimited, 
            # always return a half-full filesystem, but at least 1 TB)
            _fs_size = max(2 * _usage, 1024 ** 4)
        _bfree = ( _fs_size - _usage ) // _blocksize
        return {
            'f_bsize': _blocksize, 
            'f_frsize': _blocksize,
            'f_blocks': _fs_size // _blocksize,
            'f_bfree': _bfree,
            'f_bavail': _bfree,
            # 'f_files': c_fsfilcnt_t,
            # 'f_ffree': c_fsfilcnt_t,
            # 'f_favail': c_fsfilcnt_t

        }


    def xx_rename(self, old, new):
        return self.sftp.rename(old, self.root + new)

    def unlink(self, path):
        if path in self.__newfolders: # folder was just created, not synced yet
            self.__newfolders.remove(path)
            return
        elif path in self.__newfiles: # file was just created, not synced yet
            self.__newfiles.remove(path)
            return
        try:
            f = self._getpath(path)
        except JFS.JFSError:
            raise OSError(errno.ENOENT, '')
        r = f.delete()
        self.dirty = True

    rmdir = unlink # alias

    def write(self, path, data, offset, fh):
        if is_blacklisted(path):
            raise JottaFuseError('Blacklisted file')

        if path in self.__newfiles: # file was just created, not synced yet
            print "path: %s" % path
            f = self.client.up(path, StringIO(data))
            self.__newfiles.remove(path)
            return len(data)
        try:
            f = self._getpath(path)
        except JFS.JFSError:
            raise OSError(errno.ENOENT, '')
        olddata = f.read()
        newdata = olddata[:offset] + data
        f.write(newdata)
        return len(newdata)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print('usage: %s <mountpoint>' % sys.argv[0])
        sys.exit(1)

    fuse = FUSE(JottaFuse(username=os.environ['JOTTACLOUD_USERNAME'], password=os.environ['JOTTACLOUD_PASSWORD']), 
                sys.argv[1], foreground=True, nothreads=True)


