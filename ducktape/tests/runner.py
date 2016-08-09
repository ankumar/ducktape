# Copyright 2015 Confluent Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import logging
import multiprocessing
import os
import signal
import time
import traceback
import zmq

from ducktape.tests.serde import SerDe
from ducktape.command_line.defaults import ConsoleDefaults
from ducktape.tests.runner_client import run_client
from ducktape.tests.result import TestResults
from ducktape.utils.terminal_size import get_terminal_size
from ducktape.tests.event import ClientEventFactory, EventResponseFactory
from ducktape.cluster.finite_subcluster import FiniteSubcluster
from ducktape.tests.scheduler import TestScheduler
from ducktape.tests.result import FAIL


class Receiver(object):
    def __init__(self, port):
        self.port = port
        self.serde = SerDe()

        self.zmq_context = zmq.Context()
        self.socket = self.zmq_context.socket(zmq.REP)
        self.socket.bind("tcp://*:%s" % str(self.port))

    def recv(self):
        message = self.socket.recv()
        return self.serde.deserialize(message)

    def send(self, event):
        self.socket.send(self.serde.serialize(event))

    def close(self):
        self.socket.setsockopt(zmq.LINGER, 0)
        self.socket.close()


class TestRunner(object):
    """Runs tests serially."""

    # When set to True, the test runner will finish running/cleaning the current test, but it will not run any more
    stop_testing = False

    def __init__(self, cluster, session_context, session_logger, tests, port=ConsoleDefaults.TEST_DRIVER_PORT):
        # Set handler for SIGTERM (aka kill -15)
        # Note: it doesn't work to set a handler for SIGINT (Ctrl-C) in this parent process because the
        # handler is inherited by all forked child processes, and it prevents the default python behavior
        # of translating SIGINT into a KeyboardInterrupt exception
        signal.signal(signal.SIGTERM, self._propagate_sigterm)

        # session_logger, message logger,
        self.session_logger = session_logger
        self.cluster = cluster
        self.event_response = EventResponseFactory()
        self.hostname = "localhost"
        self.port = port
        self.receiver = Receiver(port)

        self.session_context = session_context
        self.max_parallel = session_context.max_parallel
        self.results = TestResults(self.session_context)

        self.exit_first = self.session_context.exit_first

        self.main_process_pid = os.getpid()
        self.scheduler = TestScheduler(tests, self.cluster)

        self.test_counter = 1
        self.total_tests = len(self.scheduler)
        self._test_context = {t.test_id: t for t in tests}
        self._test_cluster = {}  # Track subcluster assigned to a particular test_id
        self._client_procs = {}  # track client processes running tests
        self.active_tests = {}
        self.finished_tests = {}

    def _propagate_sigterm(self, signum, frame):
        """Handler SIGTERM and SIGINT by propagating SIGTERM to all client processes.

        Note that multiprocessing processes are in the same process group as the main process, so Ctrl-C will
        result in SIGINT being propagated to all client processes automatically. This may result in multiple SIGTERM
        signals getting sent to client processes in quick succession.

        However, it is possible that the main process (and not the process group) receives a SIGINT or SIGTERM
        directly. Propagating SIGTERM to client processes is necessary in this case.
        """
        if os.getpid() != self.main_process_pid:
            # since we're using the multiprocessing module to create client processes,
            # this signal handler is also attached client processes, so we only want to propagate TERM signals
            # if this process *is* the main runner server process
            return

        self.stop_testing = True
        for p in self._client_procs.values():

            # this handler should be a noop if we're in a client process, so it's an error if the current pid
            # is in self._client_procs
            assert p.pid != os.getpid(), "Signal handler should not reach this point in a client subprocess."
            if p.is_alive():
                os.kill(p.pid, signal.SIGTERM)

    def who_am_i(self):
        """Human-readable name helpful for logging."""
        return self.__class__.__name__

    @property
    def _ready_to_trigger_more_tests(self):
        """Should we pull another test from the scheduler?"""
        return not self.stop_testing and \
            len(self.active_tests) < self.max_parallel and \
            self.scheduler.peek() is not None

    @property
    def _expect_client_requests(self):
        return len(self.active_tests) > 0

    def run_all_tests(self):
        self.results.start_time = time.time()
        self._log(logging.INFO, "starting test run with session id %s..." % self.session_context.session_id)
        self._log(logging.INFO, "running %d tests..." % len(self.scheduler))

        while self._ready_to_trigger_more_tests or self._expect_client_requests:
            try:
                while self._ready_to_trigger_more_tests:
                    next_test_context = self.scheduler.next()
                    self._preallocate_subcluster(next_test_context)
                    self._run_single_test(next_test_context)

                if self._expect_client_requests:
                    try:
                        event = self.receiver.recv()
                        self._handle(event)
                    except Exception as e:
                        err_str = "Exception receiving message: %s: %s" % (str(type(e)), str(e))
                        err_str += "\n" + traceback.format_exc(limit=16)
                        self._log(logging.ERROR, err_str)

                        # All processes are on the same machine, so treat communication failure as a fatal error
                        raise
            except KeyboardInterrupt:
                # If SIGINT is received, stop triggering new tests, and let the currently running tests finish
                self.stop_testing = True

        for proc in self._client_procs.values():
            proc.join()
        self.receiver.close()

        return self.results

    def _run_single_test(self, test_context):
        """Start a test runner client in a subprocess"""
        self._log(logging.INFO, "Triggering test %d of %d..." % (self.test_counter, self.total_tests))
        self.test_counter += 1

        # Test is considered "active" as soon as we start it up in a subprocess
        self.active_tests[test_context.test_id] = True

        proc = multiprocessing.Process(
            target=run_client,
            args=[
                self.hostname,
                self.port,
                test_context.test_id,
                test_context.logger_name,
                test_context.results_dir,
                self.session_context.debug
            ])

        self._client_procs[test_context.test_id] = proc
        proc.start()

    def _preallocate_subcluster(self, test_context):
        """Preallocate the subcluster which will be used to run the test.

        Side effect: store association between the test_id and the preallocated subcluster.

        :param test_context
        :return None
        """
        if test_context.expected_num_nodes == len(self.cluster) and self.max_parallel > 1:
            self._log(logging.WARNING,
                      "Test %s is using entire cluster. It's possible this test has no associated cluster metadata."
                      % test_context.test_id)

        self._test_cluster[test_context.test_id] = FiniteSubcluster(self.cluster.alloc(test_context.expected_num_nodes))

    def _handle(self, event):
        self._log(logging.DEBUG, str(event))

        if event["event_type"] == ClientEventFactory.READY:
            self._handle_ready(event)
        elif event["event_type"] in [ClientEventFactory.RUNNING, ClientEventFactory.SETTING_UP, ClientEventFactory.TEARING_DOWN]:
            self._handle_lifecycle(event)
        elif event["event_type"] == ClientEventFactory.FINISHED:
            self._handle_finished(event)
        elif event["event_type"] == ClientEventFactory.LOG:
            self._handle_log(event)
        else:
            raise RuntimeError("Received event with unknown event type: " + str(event))

    def _handle_ready(self, event):
        test_id = event["test_id"]
        test_context = self._test_context[test_id]
        subcluster = self._test_cluster[test_id]
        self.receiver.send(
                self.event_response.ready(event, self.session_context, test_context, subcluster))

    def _handle_log(self, event):
        self.receiver.send(self.event_response.log(event))
        self._log(event["log_level"], event["message"])

    def _handle_finished(self, event):
        test_id = event["test_id"]
        self.receiver.send(self.event_response.finished(event))

        result = event['result']
        if result.test_status == FAIL and self.exit_first:
            self.stop_testing = True

        # Transition this test from running to finished
        del self.active_tests[test_id]
        self.finished_tests[test_id] = event
        self.results.append(result)

        # Free nodes used by the test
        subcluster = self._test_cluster[test_id]
        self.cluster.free(subcluster.alloc(len(subcluster)))
        del self._test_cluster[test_id]

        # Join on the finished test process
        self._client_procs[test_id].join()

        if self._should_print_separator:
            terminal_width, y = get_terminal_size()
            self._log(logging.INFO, "~" * int(2 * terminal_width / 3))

    @property
    def _should_print_separator(self):
        """The separator is the twiddle that goes in between tests on stdout.

        This only makes sense to print if tests are run sequentially (aka max_parallel = 1) since
        output from tests is interleaved otherwise.

        Also, we don't want to print the separator after the last test output has been received, so
        we check that there's more test output expected.
        """
        return self.session_context.max_parallel == 1 and \
            (self._expect_client_requests or self._ready_to_trigger_more_tests)

    def _handle_lifecycle(self, event):
        self.receiver.send(self.event_response._event_response(event))

    def _log(self, log_level, msg, *args, **kwargs):
        """Log to the service log of the current test."""
        self.session_logger.log(log_level, msg, *args, **kwargs)
