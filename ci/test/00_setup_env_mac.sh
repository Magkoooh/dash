#!/usr/bin/env bash
#
# Copyright (c) 2019-2020 The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.

export LC_ALL=C.UTF-8

export CONTAINER_NAME=ci_macos_cross
export HOST=x86_64-apple-darwin
export PACKAGES="cmake libz-dev python3-setuptools xorriso"
export XCODE_VERSION=15.0
export XCODE_BUILD_ID=15A240d
export RUN_UNIT_TESTS=false
export RUN_FUNCTIONAL_TESTS=false
export GOAL="all deploy"
export BITCOIN_CONFIG="--with-gui --enable-reduce-exports --disable-miner --with-boost-process"
