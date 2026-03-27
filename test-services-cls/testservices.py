"""testservices.py"""
import os
import restate
import services


def test_services():
    names = os.environ.get("SERVICES")
    return services.services_named(names.split(",")) if names else services.all_services()


e2e_signing_key_env = os.environ.get("E2E_REQUEST_SIGNING_ENV")
if e2e_signing_key_env is not None:
    e2e_signing_key_env = [e2e_signing_key_env]

app = restate.app(services=test_services(), identity_keys=e2e_signing_key_env)
