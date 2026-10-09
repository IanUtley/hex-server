"""Environment-backed profile feature flags."""

import os
import re


def _profile_feature_flags():
    """Return profile-stream feature strings sent to every authenticated client.

    The historical server always enabled the developer console with
    ``allowcon``. Keep that behavior when the environment variable is absent;
    when it is present, its comma/space/semicolon-separated values replace the
    default list so deployments can choose an explicit feature set.
    """
    raw = os.environ.get("HEX_PROFILE_FLAGS")
    if raw is None:
        return ("allowcon",)
    flags = []
    for flag in re.split(r"[,;\s]+", raw.strip()):
        if flag and flag not in flags:
            flags.append(flag)
    return tuple(flags)
