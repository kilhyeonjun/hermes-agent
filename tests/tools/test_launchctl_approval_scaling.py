"""Keep launchctl approval detection broad without quadratic regex scanning."""

import re
import statistics
import time

from tools.approval_detection import DANGEROUS_PATTERNS_COMPILED, detect_dangerous_command


_DESCRIPTION = "stop/restart hermes launchd service (kills running agents)"
_ORIGINAL = re.compile(
    r'(?=[\s\S]*\blaunchctl\s+(?:stop|kickstart|bootout|unload|kill|disable|remove)\b)'
    r'(?=[\s\S]*\b(?:hermes|ai\.hermes)\b)', re.IGNORECASE | re.DOTALL,
)


def test_launchctl_approval_pattern_preserves_order_independent_matches():
    current = next(pattern for pattern, description in DANGEROUS_PATTERNS_COMPILED if description == _DESCRIPTION)
    commands = [
        "for item in 'ai.hermes.gateway'; do launchctl bootout \"$item\"; done",
        "launchctl kickstart gui/501/ai.hermes.gateway",
        "launchctl unload /tmp/service.plist; printf hermes",
        "printf hermes; launchctl remove unrelated",
        "launchctl list; printf hermes",
        "launchctl bootout gui/501/unrelated",
        "printf unrelated",
    ]
    # Vary verb, label, order, punctuation and long benign prefixes/suffixes.
    for verb in ("stop", "kickstart", "bootout", "unload", "kill", "disable", "remove", "list"):
        for label in ("hermes", "ai.hermes.gateway", "unrelated"):
            for left, right in (("", ""), ("printf x; ", ""), ("", "; printf x")):
                commands.extend((f"{left}launchctl {verb} {label}{right}",
                                 f"{left}printf {label}; launchctl {verb} other{right}"))
    for command in commands:
        assert bool(current.search(command)) == bool(_ORIGINAL.search(command)), command
    for command in commands[:4]:
        assert detect_dangerous_command(command)[0], command


def test_launchctl_approval_pattern_throughput_relative_to_prior_regex():
    current = next(pattern for pattern, description in DANGEROUS_PATTERNS_COMPILED if description == _DESCRIPTION)
    benign = ";".join("printf neutral" for _ in range(500))
    dangerous = "printf hermes; " + benign + "; launchctl bootout other"
    assert not current.search(benign)
    assert current.search(dangerous)

    def median_seconds(pattern, command):
        samples = []
        for _ in range(3):
            started = time.perf_counter()
            pattern.search(command)
            samples.append(time.perf_counter() - started)
        return statistics.median(samples)

    # The all-benign scan exposes the repeated suffix-search regression; a
    # positive hit at offset zero already short-circuits in both implementations.
    assert median_seconds(current, benign) * 10 < median_seconds(_ORIGINAL, benign)
