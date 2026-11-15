"""Expose the standalone R1 checks to the normal hermetic unit suite."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tools/device-lease"))

from test_remote_adb import AccessTests, GatewayTests
from test_remote_attestation import AttestationTests
from test_remote_broker import BrokerRemoteTests
