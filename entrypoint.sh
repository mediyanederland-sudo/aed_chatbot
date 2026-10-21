#!/bin/sh
set -e

# Drop privileges and run the CMD as appuser
exec gosu appuser "$@"
