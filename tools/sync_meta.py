#!/usr/bin/env python
# Copyright European Organization for Nuclear Research (CERN)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#                       http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Vincent Garonne, <vincent.garonne@cern.ch>, 2013

import sys
import traceback

from rucio.client import Client
from rucio.common.exception import Duplicate

UNKNOWN = 3
CRITICAL = 2
WARNING = 1
OK = 0

if __name__ == '__main__':

    meta_keys = [('project', None, ['data13_hip', ]),
                 ('run_number', None, []),
                 ('stream_name', None, []),
                 ('prod_step', None, []),
                 ('datatype', None, []),
                 ('version', None, []),
                 ('guid', '[a-f0-9]{8}[a-f0-9]{4}[a-f0-9]{4}[a-f0-9]{4}[a-f0-9]{12}', []),
                 ('events', '^\d+$', [])]

    c = Client()
    for key, regexp, values in meta_keys:
        try:
            try:
                c.add_key(key=key, regexp=regexp)
            except Duplicate:
                print '%(key)s already added' % locals()

            for value in values:

                try:
                    c.add_value(key=key, value=value)
                except Duplicate:
                    print '%(key)s:%(value)s already added' % locals()

                if key == 'project':
                    try:
                        c.add_scope('root', value)
                    except Duplicate:
                        print 'Scope %(value)s already added' % locals()
        except:
            errno, errstr = sys.exc_info()[:2]
            trcbck = traceback.format_exc()
            print 'Interrupted processing with %s %s %s.' % (errno, errstr, trcbck)

    sys.exit(OK)
