# Copyright European Organization for Nuclear Research (CERN)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0OA
#
# Authors:
# - Vincent Garonne, <vincent.garonne@cern.ch>, 2012-2014
# - Mario Lassnig, <mario.lassnig@cern.ch>, 2013-2015
# - Cedric Serfon, <cedric.serfon@cern.ch>, 2013-2015
# - Wen Guan, <wen.guan@cern.ch>, 2014-2015

"""
Conveyor is a daemon to manage file transfers.
"""

import json
import logging
import os
import socket
import sys
import threading
import time
import traceback

from collections import defaultdict
from ConfigParser import NoOptionError

from rucio.common.config import config_get
from rucio.core import heartbeat, request
from rucio.core.monitor import record_counter, record_timer
from rucio.db.constants import RequestState

from rucio.daemons.conveyor.submitter_utils import get_rses, get_transfers_from_requests, bulk_group_transfer

logging.basicConfig(stream=sys.stdout,
                    level=getattr(logging, config_get('common', 'loglevel').upper()),
                    format='%(asctime)s\t%(process)d\t%(levelname)s\t%(message)s')

graceful_stop = threading.Event()


def submitter(once=False, rses=[],
              process=0, total_processes=1, thread=0, total_threads=1,
              mock=False, bulk=100, group_bulk=1, group_policy='rule', fts_source_strategy='auto',
              activities=None, activity_shares=None):
    """
    Main loop to submit a new transfer primitive to a transfertool.
    """

    logging.info('submitter starting - process (%i/%i) thread (%i/%i)' % (process,
                                                                          total_processes,
                                                                          thread,
                                                                          total_threads))
    try:
        scheme = config_get('conveyor', 'scheme')
    except NoOptionError:
        scheme = None

    executable = ' '.join(sys.argv)
    hostname = socket.getfqdn()
    pid = os.getpid()
    hb_thread = threading.current_thread()

    logging.info('submitter started - process (%i/%i) thread (%i/%i)' % (process,
                                                                         total_processes,
                                                                         thread,
                                                                         total_threads))

    activity_next_exe_time = defaultdict(time.time)
    while not graceful_stop.is_set():

        heartbeat.live(executable, hostname, pid, hb_thread)

        try:

            if activities is None:
                activities = [None]
            if rses:
                rse_ids = [rse['id'] for rse in rses]
            else:
                rse_ids = None

            for activity in activities:
                if activity_next_exe_time[activity] > time.time():
                    time.sleep(1)
                    continue

                logging.info("Starting submit jobs on activity: %s" % activity)
                transfers = get_transfers_from_requests(process, total_processes, thread, total_threads, rse_ids, mock, bulk, activity, activity_shares, scheme)

                # group jobs
                grouped_jobs = bulk_group_transfer(transfers, group_policy, group_bulk, fts_source_strategy)

                # submit transfers
                for external_host in grouped_jobs:
                    for job in grouped_jobs[external_host]:
                        # submit transfers
                        eid = None
                        try:
                            ts = time.time()
                            eid = request.submit_bulk_transfers(external_host, files=job['files'], transfertool='fts3', job_params=job['job_params'])
                            logging.debug("Submit job %s to %s" % (eid, external_host))
                            record_timer('daemons.conveyor.submitter.submit_bulk_transfer', (time.time() - ts) * 1000/len(job['files']))
                        except Exception, ex:
                            logging.error("Failed to submit a job with error %s: %s" % (str(ex), traceback.format_exc()))

                        # register transfer returns
                        try:
                            xfers_ret = {}
                            for file in job['files']:
                                file_metadata = file['metadata']
                                request_id = file_metadata['request_id']
                                log_str = 'COPYING REQUEST %s DID %s:%s PREVIOUS %s FROM %s TO %s USING %s ' % (file_metadata['request_id'],
                                                                                                                file_metadata['scope'],
                                                                                                                file_metadata['name'],
                                                                                                                file_metadata['previous_attempt_id'] if 'previous_attempt_id' in file_metadata else None,
                                                                                                                file['sources'],
                                                                                                                file['destinations'],
                                                                                                                external_host)
                                if eid:
                                    xfers_ret[request_id] = {'state': RequestState.SUBMITTED, 'external_host': external_host, 'external_id': eid, 'dest_url':  file['destinations'][0]}
                                    log_str += 'with state(%s) with eid(%s)' % (RequestState.SUBMITTED, eid)
                                    logging.info(log_str)
                                else:
                                    xfers_ret[request_id] = {'state': RequestState.LOST, 'external_host': external_host, 'external_id': None, 'dest_url': None}
                                    log_str += 'with state(%s) with eid(%s)' % (RequestState.LOST, None)
                                    logging.warn(log_str)
                                xfers_ret[request_id]['file'] = file
                            request.set_request_transfers(xfers_ret)
                        except Exception, ex:
                            logging.error("Failed to register transfer state with error %s: %s" % (str(ex), traceback.format_exc()))
                        record_counter('daemons.conveyor.submitter.submit_request', len(job['files']))

                if len(transfers) < group_bulk:
                    logging.info('%i:%i - only %s transfers which is less than group bulk %s, sleep 600 seconds' % (process, thread, len(transfers), group_bulk))
                    # time.sleep(60)
                    if activity_next_exe_time[activity] < time.time():
                        activity_next_exe_time[activity] = time.time() + 600
        except:
            logging.critical(traceback.format_exc())

        if once:
            return

    logging.info('graceful stop requested')

    heartbeat.die(executable, hostname, pid, hb_thread)

    logging.info('graceful stop done')


def stop(signum=None, frame=None):
    """
    Graceful exit.
    """

    graceful_stop.set()


def run(once=False,
        process=0, total_processes=1, total_threads=1, group_bulk=1, group_policy='rule',
        mock=False, rses=[], include_rses=None, exclude_rses=None, bulk=100, fts_source_strategy='auto',
        activities=[], activity_shares=None):
    """
    Starts up the conveyer threads.
    """

    if mock:
        logging.info('mock source replicas: enabled')

    working_rses = None
    if rses or include_rses or exclude_rses:
        working_rses = get_rses(rses, include_rses, exclude_rses)
        logging.info("RSE selection: RSEs: %s, Include: %s, Exclude: %s" % (rses,
                                                                            include_rses,
                                                                            exclude_rses))
    else:
        logging.info("RSE selection: automatic")

    if activity_shares:

        try:
            activity_shares = json.loads(activity_shares)
        except:
            logging.critical('activity share is not a valid JSON dictionary')
            return

        try:
            if round(sum(activity_shares.values()), 2) != 1:
                logging.critical('activity shares do not sum up to 1, got %s - aborting' % round(sum(activity_shares.values()), 2))
                return
        except:
            logging.critical('activity shares are not numbers? - aborting')
            return

        activity_shares.update((share, int(percentage*bulk)) for share, percentage in activity_shares.items())
        logging.info('activity shares enabled: %s' % activity_shares)

    if once:
        logging.info('executing one submitter iteration only')
        submitter(once,
                  rses=working_rses,
                  mock=mock,
                  bulk=bulk,
                  group_bulk=group_bulk,
                  group_policy=group_policy,
                  fts_source_strategy=fts_source_strategy,
                  activities=activities,
                  activity_shares=activity_shares)

    else:
        logging.info('starting submitter threads')
        threads = [threading.Thread(target=submitter, kwargs={'process': process,
                                                              'total_processes': total_processes,
                                                              'thread': i,
                                                              'total_threads': total_threads,
                                                              'rses': working_rses,
                                                              'bulk': bulk,
                                                              'group_bulk': group_bulk,
                                                              'group_policy': group_policy,
                                                              'activities': activities,
                                                              'fts_source_strategy': fts_source_strategy,
                                                              'mock': mock,
                                                              'activity_shares': activity_shares}) for i in xrange(0, total_threads)]

        [t.start() for t in threads]

        logging.info('waiting for interrupts')

        # Interruptible joins require a timeout.
        while len(threads) > 0:
            [t.join(timeout=3.14) for t in threads if t and t.isAlive()]