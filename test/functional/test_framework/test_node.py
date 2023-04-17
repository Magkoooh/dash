#!/usr/bin/env python3
# Copyright (c) 2017 The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""Class for dashd node under test"""

import contextlib
import decimal
import errno
from enum import Enum
import http.client
import json
import logging
import os.path
import re
import subprocess
import tempfile
import time
import urllib.parse
import shlex
import sys
import collections

from .authproxy import JSONRPCException
from .messages import MY_SUBVERSION
from .util import (
    MAX_NODES,
    append_config,
    delete_cookie_file,
    get_auth_cookie,
    get_rpc_proxy,
    rpc_url,
    wait_until,
    p2p_port,
    get_chain_folder,
    Options,
    EncodeDecimal,
)

BITCOIND_PROC_WAIT_TIMEOUT = 60


class FailedToStartError(Exception):
    """Raised when a node fails to start correctly."""


class ErrorMatch(Enum):
    FULL_TEXT = 1
    FULL_REGEX = 2
    PARTIAL_REGEX = 3


class TestNode():
    """A class for representing a dashd node under test.

    This class contains:

    - state about the node (whether it's running, etc)
    - a Python subprocess.Popen object representing the running process
    - an RPC connection to the node
    - one or more P2P connections to the node


    To make things easier for the test writer, any unrecognised messages will
    be dispatched to the RPC connection."""

    def __init__(self, i, datadir, extra_args_from_options, *, chain, rpchost, timewait, timeout_factor, bitcoind, bitcoin_cli, mocktime, coverage_dir, cwd, extra_conf=None, extra_args=None, use_cli=False, start_perf=False, use_valgrind=False):
        """
        Kwargs:
            start_perf (bool): If True, begin profiling the node with `perf` as soon as
                the node starts.
        """

        self.index = i
        self.datadir = datadir
        self.chain = chain
        self.bitcoinconf = os.path.join(self.datadir, "dash.conf")
        self.stdout_dir = os.path.join(self.datadir, "stdout")
        self.stderr_dir = os.path.join(self.datadir, "stderr")
        self.rpchost = rpchost
        self.rpc_timeout = timewait
        self.rpc_timeout *= Options.timeout_scale
        self.binary = bitcoind
        self.coverage_dir = coverage_dir
        self.cwd = cwd
        self.mocktime = mocktime
        if extra_conf is not None:
            append_config(datadir, extra_conf)
        # Most callers will just need to add extra args to the standard list below.
        # For those callers that need more flexibity, they can just set the args property directly.
        # Note that common args are set in the config file (see initialize_datadir)
        self.extra_args = extra_args
        self.extra_args_from_options = extra_args_from_options
        # Configuration for logging is set as command-line args rather than in the bitcoin.conf file.
        # This means that starting a bitcoind using the temp dir to debug a failed test won't
        # spam debug.log.
        self.args = [
            self.binary,
            "-datadir=" + self.datadir,
            "-logtimemicros",
            "-logthreadnames",
            "-debug",
            "-debugexclude=libevent",
            "-debugexclude=leveldb",
            "-mocktime=" + str(mocktime),
            "-uacomment=testnode%d" % i
        ]
        if use_valgrind:
            default_suppressions_file = os.path.join(
                os.path.dirname(os.path.realpath(__file__)),
                "..", "..", "..", "contrib", "valgrind.supp")
            suppressions_file = os.getenv("VALGRIND_SUPPRESSIONS_FILE",
                                          default_suppressions_file)
            self.args = ["valgrind", "--suppressions={}".format(suppressions_file),
                         "--gen-suppressions=all", "--exit-on-first-error=yes",
                         "--error-exitcode=1", "--quiet"] + self.args

        self.cli = TestNodeCLI(bitcoin_cli, self.datadir)
        self.use_cli = use_cli
        self.start_perf = start_perf

        # Don't try auto backups (they fail a lot when running tests)
        self.args.append("-createwalletbackups=0")

        self.running = False
        self.process = None
        self.rpc_connected = False
        self.rpc = None
        self.url = None
        self.log = logging.getLogger('TestFramework.node%d' % i)
        self.cleanup_on_exit = True # Whether to kill the node when this object goes away
        # Cache perf subprocesses here by their data output filename.
        self.perf_subprocesses = {}

        self.p2ps = []
        self.timeout_factor = timeout_factor

    AddressKeyPair = collections.namedtuple('AddressKeyPair', ['address', 'key'])
    PRIV_KEYS = [
            # address , privkey
            AddressKeyPair('yYdShjQSptFKitYLksFEUSwHe4hnbar5rf', 'cMfbiEsnG5b8Gwm6vEgfWvZLuXZNC4zsN2y7Es3An9xHRWRjmwgR'),
            AddressKeyPair('yfTFJgvq65UZsb9RBbpdYAAzsJoCGXqH2w', 'cStuFACUD1N6JjKQxNLUQ443qJUtSzLitKKEkA8x6utxTPZTLUtA'),
            AddressKeyPair('yU3w4VDjKhHiZpWszkUZVnFTS56AfgdfPV', 'cQb5yh2sTiG7dsxxbXHhWSBLMByYT7jY49A1kC7zKhgL9WNHysWW'),
            AddressKeyPair('yYhzix2R5LiYnDixsUnF8XwBYGYpyeTgB4', 'cW9Gu6uU4KoZJQcdyUvjULNRg4C8srPJw1adhgdTZMr9YQdKHtcn'),
            AddressKeyPair('yiQ3qLx5L1BW9XA6JAG7hC8UQDktcBCeYG', 'cSq7gHVC1QPsswyX2pE5C38UnWZXfCLr7XnkjnDwuZ68NkWp183T'),
            AddressKeyPair('yUL8h8mR7aNDRsU5zhcDbpp6YtA6ieUtK2', 'cTk7hiDKgxZX3JSb37vywdYYjjJows4DQjEaxBJDGF6LC6GXvPKo'),
            AddressKeyPair('yfy21e12jn3A3uDicNehCq486o9fMwJKMc', 'cMuko9rLDbtxCFWuBSrFgBDRSMxsLWKpJKScRGNuWKbhuQsnsjKT'),
            AddressKeyPair('yURgENB3b2YRMWnbhKF7iGs3KoaVRVXsJr', 'cQhdjTMh57MaHCDk9FsWGPtftRMBUuhaYAtouWnetcewmBuSrLSM'),
            AddressKeyPair('yYC9AxBEUs3ZZxfcQvj2LUF5PVxxtqaEs7', 'cQFueiiP13mfytV3Svoe4o4Ux79fRJvwuSgHapXsnBwrHod57EeL'),
            AddressKeyPair('yVs9jXGyLWLLFbpESnoppk7F8DtXcuCCTf', 'cN55daf1HotwBAgAKWVgDcoppmUNDtQSfb7XLutTLeAgVc3u8hik'),
            AddressKeyPair('yV3eqNNshZJ4Pv6NCyYsbdJb1ERFFygFqf', 'cT7qK7g1wkYEMvKowd2ZrX1E5f6JQ7TM246UfqbCiyF7kZhorpX3'),
            AddressKeyPair('yfE8gZCiFW9Uqu21v3JGibr3WVSPQWmY8n', 'cPiRWE8KMjTRxH1MWkPerhfoHFn5iHPWVK5aPqjW8NxmdwenFinJ'),
            AddressKeyPair('yLLVXzya7GzmVkjQzsCG4iDpqYJyJFDSEV', 'cVLCocFyWxzyCwEknkWvDeWneTBsh9Jf3u4yiJCYjcy3gt8Jw1cM'),
            AddressKeyPair('yLNNR3HeJxgR669oRePksYmCqHuPUG79mF', 'cQawC3oUgoToGDJBw1Ub2PpDmf44kVtcaVaTcHyzXMRKGwdn9UYW'),
            AddressKeyPair('yLPKVwRTXME7Q3JfKAPJ4FHEaGdWgJuhpj', 'cVcFaWTbkCUZPFTHfDs8iHurPWns5QXc5rqcfkPMHUdmv17o8UYB'),
            AddressKeyPair('yLPUundzTpvjU8KYVyM4Zmnr4REf3FFvhZ', 'cRVeRmRaYuEYP9HbCZFsf1ifYYZ4KQD9rttRoTNb9wjPzhvRwqMb'),
            AddressKeyPair('yLRhHqau58AS1ALtnaowv1Pyztxi1Q6fXG', 'cNYFW52pJswYbfPR9fpiRpWHEQygg5tyMih2ASPsgMgPy9SUSSEV'),
            AddressKeyPair('yLRwHeMkXwYrkDzC4q12vej243AyTeWiPm', 'cRqfZ3dAp8BJUcGhSv7ueCXNGbki1bpcXEKk5dEJN344H52GuHQY'),
            AddressKeyPair('yLTMCXJhG1mpaWhbHcsr7zUt9wDWuQSPSk', 'cVWGbeCT5QcVGVTL5NuiLs9JfL8HFDb9PN5Gq2xudw6ZsDFeDy1V'),
            AddressKeyPair('yLU9vxiAWUdiKKxn6EazLDFq9WXrK2T7RP', 'cVCzrzfxMhUMxV34UhTmdmntAqHvosAuNo2KUZsiHZSKLm73g35o'),
    ]

    def get_deterministic_priv_key(self):
        """Return a deterministic priv key in base58, that only depends on the node's index"""
        assert len(self.PRIV_KEYS) == MAX_NODES
        return self.PRIV_KEYS[self.index]

    def _node_msg(self, msg: str) -> str:
        """Return a modified msg that identifies this node by its index as a debugging aid."""
        return "[node %d] %s" % (self.index, msg)

    def _raise_assertion_error(self, msg: str):
        """Raise an AssertionError with msg modified to identify this node."""
        raise AssertionError(self._node_msg(msg))

    def __del__(self):
        # Ensure that we don't leave any dashd processes lying around after
        # the test ends
        if self.process and self.cleanup_on_exit:
            # Should only happen on test failure
            # Avoid using logger, as that may have already been shutdown when
            # this destructor is called.
            print(self._node_msg("Cleaning up leftover process"))
            self.process.kill()

    def __getattr__(self, name):
        """Dispatches any unrecognised messages to the RPC connection or a CLI instance."""
        if self.use_cli:
            return getattr(self.cli, name)
        else:
            assert self.rpc_connected and self.rpc is not None, self._node_msg("Error: no RPC connection")
            return getattr(self.rpc, name)

    def start(self, extra_args=None, *, cwd=None, stdout=None, stderr=None, **kwargs):
        """Start the node."""
        if extra_args is None:
            extra_args = self.extra_args

        # Add a new stdout and stderr file each time dashd is started
        if stderr is None:
            stderr = tempfile.NamedTemporaryFile(dir=self.stderr_dir, delete=False)
        if stdout is None:
            stdout = tempfile.NamedTemporaryFile(dir=self.stdout_dir, delete=False)
        self.stderr = stderr
        self.stdout = stdout

        if cwd is None:
            cwd = self.cwd

        all_args = self.args + self.extra_args_from_options + extra_args
        if self.mocktime != 0:
            all_args = all_args + ["-mocktime=%d" % self.mocktime]

        # Delete any existing cookie file -- if such a file exists (eg due to
        # unclean shutdown), it will get overwritten anyway by dashd, and
        # potentially interfere with our attempt to authenticate
        delete_cookie_file(self.datadir, self.chain)

        # add environment variable LIBC_FATAL_STDERR_=1 so that libc errors are written to stderr and not the terminal
        subp_env = dict(os.environ, LIBC_FATAL_STDERR_="1")

        self.process = subprocess.Popen(all_args, env=subp_env, stdout=stdout, stderr=stderr, cwd=cwd, **kwargs)

        self.running = True
        self.log.debug("dashd started, waiting for RPC to come up")

        if self.start_perf:
            self._start_perf()

    def wait_for_rpc_connection(self):
        """Sets up an RPC connection to the dashd process. Returns False if unable to connect."""
        # Poll at a rate of four times per second
        poll_per_s = 4
        for _ in range(poll_per_s * self.rpc_timeout):
            if self.process.poll() is not None:
                raise FailedToStartError(self._node_msg(
                    'dashd exited with status {} during initialization'.format(self.process.returncode)))
            try:
                rpc = get_rpc_proxy(
                    rpc_url(self.datadir, self.index, self.chain, self.rpchost),
                    self.index,
                    timeout=self.rpc_timeout // 2,  # Shorter timeout to allow for one retry in case of ETIMEDOUT
                    coveragedir=self.coverage_dir,
                )
                rpc.getblockcount()
                # If the call to getblockcount() succeeds then the RPC connection is up
                wait_until(lambda: rpc.getmempoolinfo()['loaded'])
                # Wait for the node to finish reindex, block import, and
                # loading the mempool. Usually importing happens fast or
                # even "immediate" when the node is started. However, there
                # is no guarantee and sometimes ThreadImport might finish
                # later. This is going to cause intermittent test failures,
                # because generally the tests assume the node is fully
                # ready after being started.
                #
                # For example, the node will reject block messages from p2p
                # when it is still importing with the error "Unexpected
                # block message received"
                #
                # The wait is done here to make tests as robust as possible
                # and prevent racy tests and intermittent failures as much
                # as possible. Some tests might not need this, but the
                # overhead is trivial, and the added guarantees are worth
                # the minimal performance cost.
                self.log.debug("RPC successfully started")
                if self.use_cli:
                    return
                self.rpc = rpc
                self.rpc_connected = True
                self.url = self.rpc.url
                return
            except JSONRPCException as e:  # Initialization phase
                # -28 RPC in warmup
                # -342 Service unavailable, RPC server started but is shutting down due to error
                if e.error['code'] != -28 and e.error['code'] != -342:
                    raise  # unknown JSON RPC exception
            except OSError as e:
                if e.errno == errno.ETIMEDOUT:
                    pass  # Treat identical to ConnectionResetError
                elif e.errno == errno.ECONNREFUSED:
                    pass  # Port not yet open?
                else:
                    raise  # unknown OS error
            except ValueError as e:  # cookie file not found and no rpcuser or rpcpassword; dashd is still starting
                if "No RPC credentials" not in str(e):
                    raise
            time.sleep(1.0 / poll_per_s)
        self._raise_assertion_error("Unable to connect to dashd after {}s".format(self.rpc_timeout))

    def wait_for_cookie_credentials(self):
        """Ensures auth cookie credentials can be read, e.g. for testing CLI with -rpcwait before RPC connection is up."""
        self.log.debug("Waiting for cookie credentials")
        # Poll at a rate of four times per second.
        poll_per_s = 4
        for _ in range(poll_per_s * self.rpc_timeout):
            try:
                get_auth_cookie(self.datadir, self.chain)
                self.log.debug("Cookie credentials successfully retrieved")
                return
            except ValueError:  # cookie file not found and no rpcuser or rpcpassword; bitcoind is still starting
                pass            # so we continue polling until RPC credentials are retrieved
            time.sleep(1.0 / poll_per_s)
        self._raise_assertion_error("Unable to retrieve cookie credentials after {}s".format(self.rpc_timeout))

    def generate(self, nblocks, maxtries=1000000):
        self.log.debug("TestNode.generate() dispatches `generate` call to `generatetoaddress`")
        return self.generatetoaddress(nblocks=nblocks, address=self.get_deterministic_priv_key().address, maxtries=maxtries)

    def get_wallet_rpc(self, wallet_name):
        if self.use_cli:
            return self.cli("-rpcwallet={}".format(wallet_name))
        else:
            assert self.rpc_connected and self.rpc, self._node_msg("RPC not connected")
            wallet_path = "wallet/{}".format(urllib.parse.quote(wallet_name))
            return self.rpc / wallet_path

    def version_is_at_least(self, ver):
        return self.version is None or self.version >= ver

    def stop_node(self, expected_stderr='', wait=0):
        """Stop the node."""
        if not self.running:
            return
        self.log.debug("Stopping node")
        try:
            self.stop(wait=wait)
        except http.client.CannotSendRequest:
            self.log.exception("Unable to stop node.")

        # If there are any running perf processes, stop them.
        for profile_name in tuple(self.perf_subprocesses.keys()):
            self._stop_perf(profile_name)

        # Check that stderr is as expected
        self.stderr.seek(0)
        stderr = self.stderr.read().decode('utf-8').strip()
        if stderr != expected_stderr:
            raise AssertionError("Unexpected stderr {} != {}".format(stderr, expected_stderr))

        self.stdout.close()
        self.stderr.close()

        del self.p2ps[:]

    def is_node_stopped(self):
        """Checks whether the node has stopped.

        Returns True if the node has stopped. False otherwise.
        This method is responsible for freeing resources (self.process)."""
        if not self.running:
            return True
        return_code = self.process.poll()
        if return_code is None:
            return False

        # process has stopped. Assert that it didn't return an error code.
        assert return_code == 0, self._node_msg(
            "Node returned non-zero exit code (%d) when stopping" % return_code)
        self.running = False
        self.process = None
        self.rpc_connected = False
        self.rpc = None
        self.log.debug("Node stopped")
        return True

    def wait_until_stopped(self, timeout=BITCOIND_PROC_WAIT_TIMEOUT):
        wait_until(self.is_node_stopped, timeout=timeout, timeout_factor=self.timeout_factor)

    @contextlib.contextmanager
    def assert_debug_log(self, expected_msgs, unexpected_msgs=None, timeout=2):
        if unexpected_msgs is None:
            unexpected_msgs = []
        time_end = time.time() + timeout * self.timeout_factor
        chain = get_chain_folder(self.datadir, self.chain)
        debug_log = os.path.join(self.datadir, chain, 'debug.log')
        with open(debug_log, encoding='utf-8') as dl:
            dl.seek(0, 2)
            prev_size = dl.tell()

        yield

        while True:
            found = True
            with open(debug_log, encoding='utf-8') as dl:
                dl.seek(prev_size)
                log = dl.read()
            print_log = " - " + "\n - ".join(log.splitlines())
            for unexpected_msg in unexpected_msgs:
                if re.search(re.escape(unexpected_msg), log, flags=re.MULTILINE):
                    self._raise_assertion_error('Unexpected message "{}" partially matches log:\n\n{}\n\n'.format(unexpected_msg, print_log))
            for expected_msg in expected_msgs:
                if re.search(re.escape(expected_msg), log, flags=re.MULTILINE) is None:
                    found = False
            if found:
                return
            if time.time() >= time_end:
                break
            time.sleep(0.05)
        self._raise_assertion_error('Expected messages "{}" does not partially match log:\n\n{}\n\n'.format(str(expected_msgs), print_log))

    @contextlib.contextmanager
    def profile_with_perf(self, profile_name):
        """
        Context manager that allows easy profiling of node activity using `perf`.

        See `test/functional/README.md` for details on perf usage.

        Args:
            profile_name (str): This string will be appended to the
                profile data filename generated by perf.
        """
        subp = self._start_perf(profile_name)

        yield

        if subp:
            self._stop_perf(profile_name)

    def _start_perf(self, profile_name=None):
        """Start a perf process to profile this node.

        Returns the subprocess running perf."""
        subp = None

        def test_success(cmd):
            return subprocess.call(
                # shell=True required for pipe use below
                cmd, shell=True,
                stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL) == 0

        if not sys.platform.startswith('linux'):
            self.log.warning("Can't profile with perf; only available on Linux platforms")
            return None

        if not test_success('which perf'):
            self.log.warning("Can't profile with perf; must install perf-tools")
            return None

        if not test_success('readelf -S {} | grep .debug_str'.format(shlex.quote(self.binary))):
            self.log.warning(
                "perf output won't be very useful without debug symbols compiled into bitcoind")

        output_path = tempfile.NamedTemporaryFile(
            dir=self.datadir,
            prefix="{}.perf.data.".format(profile_name or 'test'),
            delete=False,
        ).name

        cmd = [
            'perf', 'record',
            '-g',                     # Record the callgraph.
            '--call-graph', 'dwarf',  # Compatibility for gcc's --fomit-frame-pointer.
            '-F', '101',              # Sampling frequency in Hz.
            '-p', str(self.process.pid),
            '-o', output_path,
        ]
        subp = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.perf_subprocesses[profile_name] = subp

        return subp

    def _stop_perf(self, profile_name):
        """Stop (and pop) a perf subprocess."""
        subp = self.perf_subprocesses.pop(profile_name)
        output_path = subp.args[subp.args.index('-o') + 1]

        subp.terminate()
        subp.wait(timeout=10)

        stderr = subp.stderr.read().decode()
        if 'Consider tweaking /proc/sys/kernel/perf_event_paranoid' in stderr:
            self.log.warning(
                "perf couldn't collect data! Try "
                "'sudo sysctl -w kernel.perf_event_paranoid=-1'")
        else:
            report_cmd = "perf report -i {}".format(output_path)
            self.log.info("See perf output by running '{}'".format(report_cmd))

    def assert_start_raises_init_error(self, extra_args=None, expected_msg=None, match=ErrorMatch.FULL_TEXT, *args, **kwargs):
        """Attempt to start the node and expect it to raise an error.

        extra_args: extra arguments to pass through to dashd
        expected_msg: regex that stderr should match when dashd fails

        Will throw if dashd starts without an error.
        Will throw if an expected_msg is provided and it does not match dashd's stdout."""
        with tempfile.NamedTemporaryFile(dir=self.stderr_dir, delete=False) as log_stderr, \
             tempfile.NamedTemporaryFile(dir=self.stdout_dir, delete=False) as log_stdout:
            try:
                self.start(extra_args, stdout=log_stdout, stderr=log_stderr, *args, **kwargs)
                self.wait_for_rpc_connection()
                self.stop_node()
                self.wait_until_stopped()
            except FailedToStartError as e:
                self.log.debug('dashd failed to start: %s', e)
                self.running = False
                self.process = None
                # Check stderr for expected message
                if expected_msg is not None:
                    log_stderr.seek(0)
                    stderr = log_stderr.read().decode('utf-8').strip()
                    if match == ErrorMatch.PARTIAL_REGEX:
                        if re.search(expected_msg, stderr, flags=re.MULTILINE) is None:
                            self._raise_assertion_error(
                                'Expected message "{}" does not partially match stderr:\n"{}"'.format(expected_msg, stderr))
                    elif match == ErrorMatch.FULL_REGEX:
                        if re.fullmatch(expected_msg, stderr) is None:
                            self._raise_assertion_error(
                                'Expected message "{}" does not fully match stderr:\n"{}"'.format(expected_msg, stderr))
                    elif match == ErrorMatch.FULL_TEXT:
                        if expected_msg != stderr:
                            self._raise_assertion_error(
                                'Expected message "{}" does not fully match stderr:\n"{}"'.format(expected_msg, stderr))
            else:
                if expected_msg is None:
                    assert_msg = "dashd should have exited with an error"
                else:
                    assert_msg = "dashd should have exited with expected error " + expected_msg
                self._raise_assertion_error(assert_msg)

    def add_p2p_connection(self, p2p_conn, *, wait_for_verack=True, **kwargs):
        """Add a p2p connection to the node.

        This method adds the p2p connection to the self.p2ps list and also
        returns the connection to the caller."""
        if 'dstport' not in kwargs:
            kwargs['dstport'] = p2p_port(self.index)
        if 'dstaddr' not in kwargs:
            kwargs['dstaddr'] = '127.0.0.1'

        p2p_conn.peer_connect(**kwargs, net=self.chain, timeout_factor=self.timeout_factor)()
        self.p2ps.append(p2p_conn)
        if wait_for_verack:
            # Wait for the node to send us the version and verack
            p2p_conn.wait_for_verack()
            # At this point we have sent our version message and received the version and verack, however the full node
            # has not yet received the verack from us (in reply to their version). So, the connection is not yet fully
            # established (fSuccessfullyConnected).
            #
            # This shouldn't lead to any issues when sending messages, since the verack will be in-flight before the
            # message we send. However, it might lead to races where we are expecting to receive a message. E.g. a
            # transaction that will be added to the mempool as soon as we return here.
            #
            # So syncing here is redundant when we only want to send a message, but the cost is low (a few milliseconds)
            # in comparison to the upside of making tests less fragile and unexpected intermittent errors less likely.
            p2p_conn.sync_with_ping()

        return p2p_conn

    @property
    def p2p(self):
        """Return the first p2p connection

        Convenience property - most tests only use a single p2p connection to each
        node, so this saves having to write node.p2ps[0] many times."""
        assert self.p2ps, self._node_msg("No p2p connection")
        return self.p2ps[0]

    def num_connected_mininodes(self):
        """Return number of test framework p2p connections to the node."""
        return len([peer for peer in self.getpeerinfo() if peer['subver'] == MY_SUBVERSION.decode("utf-8")])

    def disconnect_p2ps(self):
        """Close all p2p connections to the node."""
        for p in self.p2ps:
            p.peer_disconnect()

        # wait for p2p connections to disappear from getpeerinfo()
        def check_peers():
            for p in self.getpeerinfo():
                for p2p in self.p2ps:
                    if p['subver'] == p2p.strSubVer.decode():
                        return False
            return True
        wait_until(check_peers, timeout=5)

        del self.p2ps[:]
        wait_until(lambda: self.num_connected_mininodes() == 0)


class TestNodeCLIAttr:
    def __init__(self, cli, command):
        self.cli = cli
        self.command = command

    def __call__(self, *args, **kwargs):
        return self.cli.send_cli(self.command, *args, **kwargs)

    def get_request(self, *args, **kwargs):
        return lambda: self(*args, **kwargs)


def arg_to_cli(arg):
    if isinstance(arg, bool):
        return str(arg).lower()
    elif isinstance(arg, dict) or isinstance(arg, list):
        return json.dumps(arg, default=EncodeDecimal)
    else:
        return str(arg)


class TestNodeCLI():
    """Interface to dash-cli for an individual node"""
    def __init__(self, binary, datadir):
        self.options = []
        self.binary = binary
        self.datadir = datadir
        self.input = None
        self.log = logging.getLogger('TestFramework.dashcli')

    def __call__(self, *options, input=None):
        # TestNodeCLI is callable with dash-cli command-line options
        cli = TestNodeCLI(self.binary, self.datadir)
        cli.options = [str(o) for o in options]
        cli.input = input
        return cli

    def __getattr__(self, command):
        return TestNodeCLIAttr(self, command)

    def batch(self, requests):
        results = []
        for request in requests:
            try:
                results.append(dict(result=request()))
            except JSONRPCException as e:
                results.append(dict(error=e))
        return results

    def send_cli(self, command=None, *args, **kwargs):
        """Run dash-cli command. Deserializes returned string as python object."""
        pos_args = [arg_to_cli(arg) for arg in args]
        named_args = [str(key) + "=" + arg_to_cli(value) for (key, value) in kwargs.items()]
        assert not (pos_args and named_args), "Cannot use positional arguments and named arguments in the same dash-cli call"
        p_args = [self.binary, "-datadir=" + self.datadir] + self.options
        if named_args:
            p_args += ["-named"]
        if command is not None:
            p_args += [command]
        p_args += pos_args + named_args
        self.log.debug("Running dash-cli command: %s" % command)
        process = subprocess.Popen(p_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        cli_stdout, cli_stderr = process.communicate(input=self.input)
        returncode = process.poll()
        if returncode:
            match = re.match(r'error code: ([-0-9]+)\nerror message:\n(.*)', cli_stderr)
            if match:
                code, message = match.groups()
                raise JSONRPCException(dict(code=int(code), message=message))
            # Ignore cli_stdout, raise with cli_stderr
            raise subprocess.CalledProcessError(returncode, self.binary, output=cli_stderr)
        try:
            return json.loads(cli_stdout, parse_float=decimal.Decimal)
        except (json.JSONDecodeError, decimal.InvalidOperation):
            return cli_stdout.rstrip("\n")
