# -*- coding: utf-8 -*-
"""
mockturtle - turtle.services configuration plugin

Copyright (c) 2014-2015 ".alyn.post." <a@turtle.email>

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
"""

import logging
from datetime import datetime
import re
import subprocess

class DeployException(object):
    pass

class DatabaseMigrator(object):
    REGISTERED_MIGRATORS = {}
    
    def __init__(self):
        pass
    
    def run(self):
        raise NotImplementedError("Must be implemented in subclass of DatabaseMigrator")

    @classmethod
    def get_migrator(cls, name):
        if name in cls.REGISTERED_MIGRATORS:
            return cls.REGISTERED_MIGRATORS[name]
        else:
            return None
    
    @classmethod
    def register_migrator(cls, name, migrator_cls):
        cls.REGISTERED_MIGRATORS[name] = migrator_cls

class DjangoDatabaseMigrator(DatabaseMigrator):
    def __init__(self):
        super(DjangoDatabaseMigrator, self).__init__()
        
    def run(self):
        argv = ['python',
                'manage.py',
                'makemigrations'
        ]

        proc = subprocess.Popen(argv,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
        stdout, stderr = proc.communicate()
        
        proc.wait()
        status = proc.returncode
        
        return (status, stdout, stderr)

DatabaseMigrator.register_migrator('django', DjangoDatabaseMigrator)

class DeployLib(object):
    def __init__(self, product_prefix=None, db_migrator=None):
        self.product_prefix = product_prefix
        self.db_migrator_name = db_migrator
        if self.db_migrator_name:
            self.migrator_cls = DatabaseMigrator.get_migrator(self.db_migrator_name)
            self.no_migrator = False
        else:
            self.no_migrator = True

        if not self.product_prefix:
            try:
                with open("./PRODUCT", 'r') as f:
                    prod = f.read().strip()
                    self.product_prefix = prod
            except:
                msg = "Unable to find or open file [%s]. Could not compute release id." % "./PRODUCT"
                logging.error(msg)
                raise DeployException("Could not determine product to deploy. Please either specify a --product argument to this script or a PRODUCT file in the repo root.")
    
    def run_db_migrations(self):
        if self.no_migrator:
            return (0, "No db migrator specified.", "")
        else:
            if self.migrator_cls:
                migrator_inst = self.migrator_cls()
                return migrator_inst.run()
            else:
                return (1, "", "Could not find db migrator [%s]" % self.db_migrator_name)
    
    def intuit_git_commit_trunc_hash(self):
        argv = ['git',
                '--git-dir=.git',
                '--work-tree=.',
                'log',
                '-n',
                '1',
                '-q']
        log_entry = subprocess.check_output(argv, stderr=subprocess.STDOUT)
        commit_hash = log_entry.split("\n")[0].split(" ")[1]
        return commit_hash[0:7]

    def intuit_git_branch(self):
        argv = ['git',
                '--git-dir=.git',
                '--work-tree=.',
                'symbolic-ref',
                '-q',
                'HEAD']
    
        branch = subprocess.check_output(argv, stderr=subprocess.STDOUT)
        branch = branch.rstrip("\n")
        if branch.startswith('refs/heads/'):
            branch = branch[len('refs/heads/'):]
        return branch

    def gen_release_id(self, stack_name, stamp, is_blessed):
        branch = self.intuit_git_branch()
        trunc_hash = self.intuit_git_commit_trunc_hash()
        pkg_name = None
        pkg_ver = None

        # Check to see if the branch includes a sym ver (symbolic version number)
        m = re.match("(.+)(\d+\.\d+\.\d+)$", branch)
        if m:
            branch_prefix = m.group(1)
            sym_ver = m.group(2)
        else:
            logging.info("Git branch does not contain sym ver, checking for VERSION file.")
            branch_prefix = branch
            
            # Attempt to fetch sym ver from ./VERSION
            try:
                with open("./VERSION", 'r') as f:
                    sym_ver = f.read().strip()
            except:
                msg = "Unable to find or open file [%s]. Could not compute release id." % "./VERSION"
                logging.error(msg)
                return None
        
        pkg_name = "%s-%s" % (self.product_prefix, branch_prefix)
        
        if is_blessed:
            release_id = "%s-%s-%s" % (self.product_prefix, branch_prefix, sym_ver)
            pkg_ver = sym_ver
        else:
            d = datetime.fromtimestamp(int(stamp))
            datestr = d.strftime("%Y%m%dT%H%M%S")
            
            release_id = "%s-%s-%s-%s-%s" % (self.product_prefix, branch_prefix, sym_ver, datestr, trunc_hash)
            pkg_ver = "%s+%s.%s" % (sym_ver, datestr, trunc_hash)
        
        return (release_id, pkg_name, pkg_ver)